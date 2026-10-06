#!/bin/bash
# Create the FIM engine container: a small base model with FIM training, served by vLLM
# (only /v1/completions). The systemd unit starts it; this script only (re)creates it.
#
# Why each flag:
#   --gpu-memory-utilization + KV fp8   it shares a card with a bigger model; fp8 cache
#                         doubles the tokens that fit in the gap.
#   --max-model-len 4096  the proxy trims to 3000 chars before and 800 after (~1.2K tokens).
#   --max-num-seqs 2      Copilot overlaps requests while you type.
#   repetition_penalty    without it, Granite loops until max_tokens (invented table rows,
#                         the same sentence twice). 1.1 does not stop the tables; 1.2 starts
#                         to invent. It is only the default: a client that sends one wins.
#   --restart no          granite-fim.service starts it, after the bigger model (see the unit).
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
[ -f "$DIR/env" ] && . "$DIR/env"
IMAGE=${IMAGE:-vllm/vllm-openai:latest}
MODELS_DIR=${MODELS_DIR:-/srv/models}
MODEL=${MODEL:-/models/granite-4.1-8b-base-w4a16}
SERVED_NAME=${SERVED_NAME:-granite-4.1-8b-fim}
PORT=${PORT:-8002}
RENDER_NODE=${RENDER_NODE:-}
GPU_ARGS=${GPU_ARGS:-}
GPU_MEM=${GPU_MEM:-0.21}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-4096}
MAX_NUM_SEQS=${MAX_NUM_SEQS:-2}
REPETITION_PENALTY=${REPETITION_PENALTY:-1.15}

if [ -n "$RENDER_NODE" ]; then
  GPU_ARGS="--device /dev/kfd --device $RENDER_NODE --group-add $(getent group video | cut -d: -f3) --group-add $(getent group render | cut -d: -f3) $GPU_ARGS"
fi

# Stop the unit before removing the container, or Restart=on-failure brings it back half-made.
sudo systemctl stop granite-fim.service
docker rm -f granite-fim 2>/dev/null || true
while docker ps -a --format '{{.Names}}' | grep -qx granite-fim; do sleep 2; done

# shellcheck disable=SC2086  # GPU_ARGS is a list of flags
docker create --name granite-fim \
  --restart no \
  $GPU_ARGS \
  --ipc=host --shm-size 2g \
  -v "$MODELS_DIR":/models:ro \
  -v "$DIR/cache":/cache \
  -p "$PORT":8000 \
  --health-cmd "curl -f http://localhost:8000/health || exit 1" \
  --health-start-period 300s --health-interval 60s --health-retries 3 \
  -e VLLM_CACHE_ROOT=/cache/vllm -e HOME=/cache -e HSA_TOOLS_DISABLE_REGISTER=1 \
  "$IMAGE" "$MODEL" \
    --served-model-name "$SERVED_NAME" \
    --host 0.0.0.0 --port 8000 \
    --max-model-len "$MAX_MODEL_LEN" --max-num-seqs "$MAX_NUM_SEQS" \
    --gpu-memory-utilization "$GPU_MEM" --kv-cache-dtype fp8 \
    --override-generation-config "{\"repetition_penalty\": $REPETITION_PENALTY}"
sudo systemctl start --no-block granite-fim.service
echo "granite-fim created; granite-fim.service starts it."
echo "  ready when: curl -s http://127.0.0.1:$PORT/v1/models"
