#!/usr/bin/env python3
"""Translate Copilot inline-completion requests into FIM calls to your own models.

Copilot (with github.copilot.advanced.debug.overrideProxyUrl) POSTs to
/v1/engines/<engine>/completions with prompt and suffix split apart and its own
GitHub token. The models want one FIM prompt (and the upstream, its own key).

Backends are tried in order: typically a big model on a LAN server (UPSTREAM1),
then a small local one (UPSTREAM2) for when the server is out of reach.
"""
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Extra CA for an upstream with a private certificate; empty uses the system store.
CA = os.environ.get("CA_BUNDLE") or None
LISTEN = ("127.0.0.1", int(os.environ.get("PORT", "8788")))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "64"))
# Copilot sends whole-file context; the model re-reads it on every keystroke.
MAX_PREFIX = int(os.environ.get("MAX_PREFIX_CHARS", "3000"))
MAX_SUFFIX = int(os.environ.get("MAX_SUFFIX_CHARS", "800"))
# After a backend fails to connect, skip it for this long so we don't wait on every keystroke.
SKIP_SECONDS = int(os.environ.get("SKIP_SECONDS", "60"))


def backend(n):
    url = os.environ.get(f"UPSTREAM{n}")
    if not url:
        return None
    return {"name": os.environ.get(f"NAME{n}", url), "url": url, "model": os.environ[f"MODEL{n}"],
            "key": os.environ.get(f"KEY{n}", ""),
            "connect_timeout": float(os.environ.get(f"CONNECT_TIMEOUT{n}", "1.5")),
            "down_until": 0.0}


BACKENDS = [b for b in (backend(1), backend(2)) if b]
LOCK = threading.Lock()

# vLLM rejects more than 4 stop strings, and the gateway then retries WITHOUT any stop,
# so "\n" stopped working. Copilot's own stops go first; the model ends on <|endoftext|> anyway.
MAX_STOPS = 4
FIM_STOPS = ["<|fim_prefix|>", "<|file_sep|>", "<|fim_suffix|>", "<|fim_middle|>"]


def stops_for(client_stops):
    out = []
    for s in list(client_stops or []) + FIM_STOPS:
        if s and s not in out:
            out.append(s)
    return out[:MAX_STOPS]

CTX = ssl.create_default_context(cafile=CA)
log = logging.getLogger("copilot-fim-proxy")


def trim_prefix(text):
    # Cut at a line start so the model doesn't see half a line.
    if len(text) <= MAX_PREFIX:
        return text
    cut = text[-MAX_PREFIX:]
    return cut[cut.find("\n") + 1:]


def trim_suffix(text):
    if len(text) <= MAX_SUFFIX:
        return text
    cut = text[:MAX_SUFFIX]
    return cut[:cut.rfind("\n") + 1] or cut


def fix_chunk(line):
    # The final chunk carries finish_reason without "text"; Copilot expects the field.
    if not line.startswith(b"data: {"):
        return line
    try:
        d = json.loads(line[6:])
    except ValueError:
        return line
    for c in d.get("choices", []):
        c.setdefault("text", "")
    return b"data: " + json.dumps(d).encode() + b"\n"


LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?")


def line_key(line):
    # The first line starts at the cursor, after the "- [ ] "; the others carry it.
    return LIST_MARK.sub("", line).strip()


def worth_checking(key):
    # "}", "end" or "- [ ]" repeat for good reasons; a line with real words does not.
    return len(key) >= 6 and any(ch.isalpha() for ch in key)


class LoopGuard:
    """Cut the completion where it repeats one of its own lines.

    Granite fills the 64 tokens alternating two list items ("make breakfast",
    "make lunch", "make breakfast"...) even with repetition_penalty 1.15 in the
    engine. Text goes out one whole line at a time, and the
    newline after a line only once the next line is accepted, so a cut leaves none.
    """

    def __init__(self):
        self.pending = ""
        self.seen = set()
        self.cut = False
        self.newline_owed = False

    def feed(self, text):
        """Return the text that can go out now. After a cut, always ''."""
        if self.cut:
            return ""
        self.pending += text
        out = ""
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            key = line_key(line)
            if worth_checking(key) and key in self.seen:
                self.cut = True
                self.pending = ""
                return out
            self.seen.add(key)
            out += ("\n" if self.newline_owed else "") + line
            self.newline_owed = True
        return out

    def finish(self):
        """The unfinished last line: dropped if it is the start of a line already sent."""
        rest, self.pending = self.pending, ""
        if self.cut:
            return ""
        key = line_key(rest)
        if key and any(s.startswith(key) for s in self.seen if worth_checking(s)):
            self.cut = True
            return ""
        return ("\n" if self.newline_owed else "") + rest


def open_upstream(body):
    """Return (backend, response) from the first backend that answers, or raise the last error."""
    now = time.monotonic()
    candidates = [b for b in BACKENDS if b["down_until"] <= now] or BACKENDS
    err = None
    for b in candidates:
        headers = {"Content-Type": "application/json"}
        if b["key"]:
            headers["Authorization"] = f"Bearer {b['key']}"
        req = urllib.request.Request(b["url"], json.dumps({**body, "model": b["model"]}).encode(), headers)
        try:
            # Fail fast when the host is unreachable (away from home), but give the model time.
            u = urllib.parse.urlsplit(b["url"])
            socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)),
                                     timeout=b["connect_timeout"]).close()
            resp = urllib.request.urlopen(req, timeout=30,
                                          context=CTX if b["url"].startswith("https") else None)
            if b["down_until"]:
                log.info("%s is back", b["name"])
                with LOCK:
                    b["down_until"] = 0.0
            return b, resp
        except urllib.error.HTTPError:
            raise  # the backend answered; its error is the answer
        except Exception as e:
            log.warning("%s unreachable (%s), skipping it for %ds", b["name"], e, SKIP_SECONDS)
            with LOCK:
                b["down_until"] = time.monotonic() + SKIP_SECONDS
            err = e
    raise err or RuntimeError("no backends configured")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log.debug(format, *args)

    def _reply(self, code, body=b"{}", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        log.info("GET %s (ignored)", self.path)
        self._reply(200)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self.path.rstrip("/").endswith("/completions") or "/chat/" in self.path:
            log.info("POST %s (ignored, %d bytes)", self.path, len(raw))
            self._reply(404)
            return
        req = json.loads(raw or b"{}")
        stream = bool(req.get("stream"))
        prefix = trim_prefix(req.get("prompt", ""))
        suffix = trim_suffix(req.get("suffix", ""))
        body = {
            "prompt": f"<|fim_prefix|>{prefix}<|fim_suffix|>{suffix}<|fim_middle|>",
            "max_tokens": min(int(req.get("max_tokens") or MAX_TOKENS), MAX_TOKENS),
            "temperature": req.get("temperature", 0.1),
            "top_p": req.get("top_p", 1),
            "n": 1,
            "stop": stops_for(req.get("stop")),
            "stream": stream,
        }
        t0 = time.monotonic()
        try:
            b, resp = open_upstream(body)
        except urllib.error.HTTPError as e:
            err = e.read()
            log.warning("upstream %s: %s", e.code, err[:300])
            self._reply(e.code, err)
            return
        except Exception as e:
            self._reply(502, json.dumps({"error": str(e)}).encode())
            return
        log.info("%s: prompt=%d->%d suffix=%d->%d chars, stop=%s, first byte %.2fs", b["name"],
                 len(req.get("prompt", "")), len(prefix), len(req.get("suffix", "")), len(suffix),
                 json.dumps(req.get("stop")), time.monotonic() - t0)
        with resp:
            if not stream:
                d = json.loads(resp.read())
                for c in d.get("choices", []):
                    g = LoopGuard()
                    text = g.feed(c.get("text") or "") + g.finish()
                    if g.cut:
                        log.info("loop cut: %d -> %d chars", len(c.get("text") or ""), len(text))
                        c["text"], c["finish_reason"] = text, "stop"
                self._reply(200, json.dumps(d).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            guard = LoopGuard()
            try:
                for line in resp:
                    if not line.startswith(b"data: {"):
                        if line.startswith(b"data: [DONE]"):
                            self._flush_guard(guard)
                        self.wfile.write(line)
                        self.wfile.flush()
                        continue
                    d = json.loads(fix_chunk(line)[6:])
                    c = (d.get("choices") or [{}])[0]
                    done = c.get("finish_reason") is not None
                    c["text"] = guard.feed(c.get("text", ""))
                    if done:
                        c["text"] += guard.finish()
                    if guard.cut:
                        c["finish_reason"] = "stop"
                        log.info("loop cut")
                    self.last_chunk = d
                    if c["text"] or done or guard.cut:
                        self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n")
                        self.wfile.flush()
                    if guard.cut:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return  # closing resp aborts the generation upstream
            except (BrokenPipeError, ConnectionResetError):
                pass  # Copilot cancels requests while you keep typing

    def _flush_guard(self, guard):
        # [DONE] without a finish_reason chunk before it: send what the guard still holds.
        rest = guard.finish()
        if rest and getattr(self, "last_chunk", None):
            d = self.last_chunk
            d["choices"][0]["text"] = rest
            self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n")


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("listening on %s:%d -> %s", *LISTEN, " then ".join(f"{b['name']} ({b['model']})" for b in BACKENDS))
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
