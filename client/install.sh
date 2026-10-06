#!/bin/sh
# Install the client for the current user: links the proxy and the units to this
# checkout, so a `git pull` plus a restart is the whole update.
set -eu
here=$(cd "$(dirname "$0")" && pwd)

if [ -d ~/.local/share/copilot-fim-proxy ] && [ ! -L ~/.local/share/copilot-fim-proxy ]; then
  echo "~/.local/share/copilot-fim-proxy is a real folder: move it away first." >&2
  exit 1
fi
mkdir -p ~/.local/share ~/.config/copilot-fim-proxy ~/.config/systemd/user
ln -sfn "$here" ~/.local/share/copilot-fim-proxy
if [ ! -e ~/.config/copilot-fim-proxy/env ]; then
  install -m 600 "$here/env.example" ~/.config/copilot-fim-proxy/env
  echo "Edit ~/.config/copilot-fim-proxy/env before starting the proxy."
fi
for u in "$here"/systemd/*.service; do
  ln -sfn "$u" ~/.config/systemd/user/
done
systemctl --user daemon-reload
echo "Then: systemctl --user enable --now ollama-autocomplete copilot-fim-proxy"
echo "      OLLAMA_HOST=127.0.0.1:11435 ollama pull qwen2.5-coder:1.5b-base"
