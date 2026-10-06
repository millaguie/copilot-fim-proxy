# copilot-fim-proxy

Use GitHub Copilot's inline completions in VS Code with your own models.
Nothing you type goes to GitHub's completion service.

```
VS Code (Copilot) ──► copilot-fim-proxy (127.0.0.1:8788) ─┬─► 1) FIM model on a LAN server (vLLM)
                                                          └─► 2) small FIM model on this machine (Ollama)
```

- `client/`: the proxy (Python, standard library only), its systemd user units and the VS Code settings.
- `server/`: a vLLM container for the FIM model, with a systemd unit that starts it after another
  model on the same GPU.

## How it works

Copilot has a debug setting, `debug.overrideProxyUrl`, that sends its completion requests to another
URL. It sends `POST /v1/engines/<engine>/completions` with the text before the cursor (`prompt`) and the
text after it (`suffix`) as separate fields, plus its GitHub token. The proxy:

1. Trims the context. Copilot sends the whole file; the proxy keeps 3000 characters before the cursor
   and 800 after, cut at line starts.
2. Builds one FIM prompt (fill in the middle: the model writes the gap between prefix and suffix):
   `<|fim_prefix|>…<|fim_suffix|>…<|fim_middle|>`.
3. Sends it to the first backend that answers, with that backend's key.
4. Stops the completion where it repeats one of its own lines (see [Loops](#loops)).

If a backend does not accept a connection within 1.5 s, the proxy skips it for 60 s. Away from the LAN,
the first request takes about 1.6 s and the next ones go straight to the local model.

> ⚠️ `debug.overrideProxyUrl` is a debug setting, not a supported option. A Copilot update can break it
> without notice. If the proxy log shows no requests, check that first. Copilot still needs a GitHub
> sign-in to start.

## Client setup

Requirements: Python 3.9+, systemd, [Ollama](https://ollama.com) for the local fallback, and the
GitHub Copilot extension.

```bash
client/install.sh
$EDITOR ~/.config/copilot-fim-proxy/env          # backends, models, key
systemctl --user enable --now ollama-autocomplete copilot-fim-proxy
OLLAMA_HOST=127.0.0.1:11435 ollama pull qwen2.5-coder:1.5b-base
```

`install.sh` links `~/.local/share/copilot-fim-proxy` and the units to this checkout. To update:
`git pull` and `systemctl --user restart copilot-fim-proxy`.

Add [`client/vscode-settings.json`](client/vscode-settings.json) to your VS Code user settings:

```json
"github.copilot.advanced": {
    "debug.overrideProxyUrl": "http://127.0.0.1:8788",
    "debug.overrideEngine": "fim"
},
"github.copilot.nextEditSuggestions.enabled": false
```

Next edit suggestions use a different GitHub model that does not go through the proxy, so turn them off.

Watch it work:

```bash
journalctl --user -u copilot-fim-proxy -f
```

Each request logs the backend that served it, how much context was trimmed, the `stop` strings Copilot
asked for and the time to first byte.

### Configuration

All in `~/.config/copilot-fim-proxy/env` (see [`client/env.example`](client/env.example)):

| Variable | Default | |
|---|---|---|
| `UPSTREAMn`, `MODELn` | | Backend `n` (1 or 2): a `/v1/completions` URL and the model name |
| `KEYn` | | Bearer key for backend `n` |
| `NAMEn` | the URL | Name in the log |
| `CONNECT_TIMEOUTn` | `1.5` | Seconds to wait for a TCP connection |
| `CA_BUNDLE` | system store | CA file for an upstream with a private certificate |
| `PORT` | `8788` | Local port |
| `MAX_PREFIX_CHARS` / `MAX_SUFFIX_CHARS` | `3000` / `800` | Context sent to the model |
| `MAX_TOKENS` | `64` | Upper limit for each completion |
| `SKIP_SECONDS` | `60` | How long to skip a backend that did not connect |

## Local fallback model

The second backend runs on the same machine as VS Code. It answers when the LAN server is out of
reach, so it must be fast enough on that machine's own hardware.

### How to choose it

A local model is useful only if it meets all of these conditions:

1. **It is a base model trained with FIM.** Use the `-base` tags in Ollama. Instruct and chat models
   apply a chat template to the prompt and return empty text or junk (see
   [Choosing a model](#choosing-a-model)).
2. **It answers in less than about 1 second while you type.** A slower completion arrives after you
   have typed the next character, and it is no longer useful.
3. **It fits in memory next to everything else.** With `OLLAMA_KEEP_ALIVE=-1` it stays loaded all
   the time.

Speed depends mostly on the prompt, not on the completion. The proxy sends about 1,200 tokens of
context on each keystroke and asks for 64 tokens at most. So look at the prompt speed (tokens per
second) of your hardware first.

Measured on a laptop with a Radeon 890M iGPU (32 GB shared memory), Ollama with ROCm:

| Model | Prompt / generation | First request | While typing (prompt cache) | Quality |
|---|---|---|---|---|
| `qwen2.5-coder:1.5b-base` (iGPU) | ~800 / ~30 tok/s | 2-3.5 s | **0.6-1.1 s** | fair in prose, good in code |
| `qwen2.5-coder:7b-base` (iGPU) | ~200 / 5-10 tok/s | 6-12 s | ~2 s | much better, but too late |
| `qwen2.5-coder:1.5b-base` (CPU only) | ~180 / ~16 tok/s | | | too slow |

`qwen2.5-coder:3b-base` sits between the two; it was not measured on the iGPU.

Rules of thumb:

- **iGPU or CPU only:** `qwen2.5-coder:1.5b-base`. Nothing bigger answers in time.
- **A discrete GPU with 6 GB or more:** try `qwen2.5-coder:3b-base` or `qwen2.5-coder:7b-base` and
  measure. With a good GPU, the local model can be your first backend and you do not need a server.
- **Mostly prose, not code:** Granite 4.1-8B-Base is much better than Qwen2.5-Coder (see the table
  in [Choosing a model](#choosing-a-model)), but it needs a real GPU. Serve it with vLLM or llama.cpp
  (GGUF), not with the `ollama pull` tags above.

### Try a model

The Ollama for autocomplete runs as its own user service, `ollama-autocomplete.service`:

- Port 11435, so it does not clash with a system Ollama on 11434.
- Models in `~/.local/share/ollama-autocomplete`, so it does not need write access to the system
  Ollama's model folder.
- `OLLAMA_KEEP_ALIVE=-1`: the model never unloads, so there is no cold start while you type.
- `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1`: one model, one request at a time.
- `OLLAMA_CONTEXT_LENGTH=4096`: enough for the trimmed context.
- `OLLAMA_IGPU_ENABLE=1`: let Ollama use an integrated GPU.

Download the model and time one FIM request with a real file as context. Run it twice: the second
one shows the speed with the prompt cache, which is what you get while typing.

```bash
export OLLAMA_HOST=127.0.0.1:11435
ollama pull qwen2.5-coder:3b-base

python3 - README.md qwen2.5-coder:3b-base <<'EOF'
import json, sys, time, urllib.request
text = open(sys.argv[1]).read()
mid = len(text) // 2
prompt = f"<|fim_prefix|>{text[:mid][-3000:]}<|fim_suffix|>{text[mid:][:800]}<|fim_middle|>"
for _ in range(2):
    t = time.monotonic()
    req = urllib.request.Request("http://127.0.0.1:11435/v1/completions", json.dumps(
        {"model": sys.argv[2], "prompt": prompt, "max_tokens": 64, "temperature": 0.1}).encode(),
        {"Content-Type": "application/json"})
    out = json.load(urllib.request.urlopen(req))["choices"][0]["text"]
    print(f"{time.monotonic() - t:.2f}s {out!r}")
EOF
```

If the second time is under 1 second and the text makes sense, use it. Set `MODEL2` in
`~/.config/copilot-fim-proxy/env` and restart the proxy:

```bash
systemctl --user restart copilot-fim-proxy
```

### Without Ollama

Any server with an OpenAI-style `/v1/completions` endpoint that passes the prompt as is works, for
example `llama-server` from llama.cpp. Point `UPSTREAM2` to it and remove `ollama-autocomplete` from
`Wants=` in `copilot-fim-proxy.service`.

## Server setup

`server/run.sh` creates a vLLM container named `granite-fim`. `granite-fim.service` starts it.

```bash
sudo mkdir -p /opt/granite-fim
sudo cp server/run.sh server/wait-for.sh /opt/granite-fim/
sudo cp server/env.example /opt/granite-fim/env       # then edit it
sudo cp server/systemd/granite-fim.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable granite-fim
/opt/granite-fim/run.sh
```

The defaults fit a 32 GB AMD card shared with a bigger model: 21 % of the memory and an fp8 KV cache.
If the FIM model is alone on its card, raise `GPU_MEM`.

> 🔴 **Start the bigger model first.** vLLM checks at start that its share of the GPU is free. If the
> small model takes its memory first, the big one fails and restarts in a loop. Set `WAIT_FOR` to the
> big model's container: the unit waits up to 15 minutes for it to be healthy.

Put a gateway (for example LiteLLM) in front if you need keys. The proxy only needs a
`/v1/completions` endpoint.

## Choosing a model

You need a **base** model trained with FIM. Chat and instruct models return empty or junk text with FIM
tokens. Qwen3 and Qwen3.5 Base have the FIM tokens in the tokenizer but were not trained with them.

Measured with 30 gaps of 2-6 words in Spanish prose and 10 in code, judged by a bigger model. With 30
cases, differences under about 15 points are noise.

| Model | Prose: fits | Code: fits | p50 latency |
|---|---|---|---|
| **Granite 4.1-8B-Base** (vLLM, GPTQ W4A16) | **73 %** | 60 % | 183 ms |
| Granite 4.1-8B-Base (llama.cpp Q8_0) | 70 % | 60 % | 279 ms |
| Qwen2.5-Coder-3B (llama.cpp Q8_0) | 57 % | 50 % | 148 ms |
| Qwen2.5-Coder-7B (llama.cpp Q4_K_M) | 53 % | **80 %** | 178 ms |
| Qwen2.5-Coder-1.5B (vLLM bf16) | 20 % | 50 % | 154 ms |
| Codestral-22B v0.1 (llama.cpp Q4_K_M) | 20 % | 30 % | 748 ms |
| Qwen3.5-2B-Base, Qwen3-1.7B-Base | no FIM | no FIM | |

For prose, Granite 4.1-8B-Base (multilingual, FIM in its model card, the same FIM tokens as Qwen). For
mostly code, Qwen2.5-Coder-7B. Without a suffix (prefix only) every model fits 10-30 % of prose gaps:
real FIM doubles or triples that.

On a laptop iGPU (Radeon 890M), Qwen2.5-Coder-1.5B gives about 0.6 s per keystroke and the 7B about
2 s. The 1.5B is the fallback: fair in prose, good in code.

## Loops

Small base models repeat themselves until `max_tokens`. Two fixes, one on each side:

- **Server:** `repetition_penalty 1.15` as the default (`--override-generation-config`). 1.1 does not
  stop invented table rows; 1.2 starts to invent code. 1.15 stops the loops and keeps legitimate
  repeats (`self.x = x`).
- **Proxy:** that penalty does not stop a list that alternates two items. `LoopGuard` cuts the
  completion where a line repeats an earlier one. It ignores list markers (`- [ ] `, `* `, `1. `) and
  lines shorter than 6 characters or with no letters, so `}` or `end` can repeat. The proxy sends text
  one whole line at a time. On a cut, it closes the upstream connection and tells Copilot
  `finish_reason: stop`. The log shows `loop cut`.

> 🔴 **vLLM accepts at most 4 `stop` strings.** With more, it returns 400. A LiteLLM gateway then retries
> *without any* `stop`: the request works, but Copilot's `\n` is lost and the completion runs over
> several lines. The proxy sends Copilot's stops first and fills up to 4 with FIM tokens.

## Tests

```bash
python3 -m unittest discover -s tests
```

## What did not work

- **Continue 2.0** against a LiteLLM gateway: Next Edit is on by default and does not do FIM. Even with it
  off, it showed no completions and blocked the extension host while typing in big files.
- **A chat model** (Qwen3.6-35B-A3B) for FIM: fast, but almost always empty.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
