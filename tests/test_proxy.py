import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
import proxy  # noqa: E402


def guard(text):
    g = proxy.LoopGuard()
    return g.feed(text) + g.finish(), g.cut


class LoopGuardTest(unittest.TestCase):
    def test_cuts_alternating_list_items(self):
        out, cut = guard("make breakfast\n- [ ] make lunch\n- [ ] make breakfast\n- [ ] make lunch")
        self.assertTrue(cut)
        self.assertEqual(out, "make breakfast\n- [ ] make lunch")

    def test_keeps_short_repeated_lines(self):
        text = "    }\n}\n    }\n}"
        self.assertEqual(guard(text), (text, False))

    def test_drops_unfinished_repeat(self):
        out, cut = guard("restart the service\nrestart the")
        self.assertTrue(cut)
        self.assertEqual(out, "restart the service")

    def test_streamed_in_pieces(self):
        g = proxy.LoopGuard()
        out = "".join(g.feed(p) for p in ["first line he", "re\nsecond li", "ne\nfirst line here\n"])
        self.assertTrue(g.cut)
        self.assertEqual(out, "first line here\nsecond line")


class HelpersTest(unittest.TestCase):
    def test_stops_client_first_max_four(self):
        self.assertEqual(proxy.stops_for(["\n", "\n\n"]), ["\n", "\n\n", "<|fim_prefix|>", "<|file_sep|>"])
        self.assertEqual(len(proxy.stops_for(None)), proxy.MAX_STOPS)

    def test_trim_prefix_starts_at_line(self):
        text = "x" * 10 + "\n" + "a\n" * proxy.MAX_PREFIX
        cut = proxy.trim_prefix(text)
        self.assertLessEqual(len(cut), proxy.MAX_PREFIX)
        self.assertTrue(cut.startswith("a\n"))

    def test_trim_suffix_ends_at_line(self):
        cut = proxy.trim_suffix("b\n" * proxy.MAX_SUFFIX)
        self.assertTrue(cut.endswith("\n"))


class FakeEngine(BaseHTTPRequestHandler):
    """Answers like vLLM /v1/completions, streaming a looping completion."""
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeEngine.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for t in ["one item\n", "two item\n", "one item\n", "two item\n"]:
            self.wfile.write(b"data: " + json.dumps({"choices": [{"text": t, "finish_reason": None}]}).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


class EndToEndTest(unittest.TestCase):
    def test_copilot_request(self):
        engine = ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
        threading.Thread(target=engine.serve_forever, daemon=True).start()
        proxy.BACKENDS[:] = [{"name": "fake", "url": f"http://127.0.0.1:{engine.server_port}/v1/completions",
                              "model": "m", "key": "", "connect_timeout": 1, "down_until": 0.0}]
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        body = {"prompt": "before", "suffix": "after", "stream": True, "stop": ["\n\n"], "max_tokens": 500}
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/engines/x/completions",
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            chunks = [json.loads(l[6:]) for l in r.read().split(b"\n") if l.startswith(b"data: {")]
        for s in (server, engine):
            s.shutdown()
            s.server_close()

        sent = FakeEngine.seen[-1]
        self.assertEqual(sent["prompt"], "<|fim_prefix|>before<|fim_suffix|>after<|fim_middle|>")
        self.assertEqual(sent["model"], "m")
        self.assertEqual(sent["max_tokens"], proxy.MAX_TOKENS)
        self.assertEqual(sent["stop"][0], "\n\n")
        self.assertEqual("".join(c["choices"][0]["text"] for c in chunks), "one item\ntwo item")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
