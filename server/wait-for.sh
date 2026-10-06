#!/bin/sh
# ExecStartPre of granite-fim.service: wait until the container in WAIT_FOR is healthy.
# vLLM checks at start that its share of the GPU is free; if the small model grabs
# memory first, the big one on the same card fails and restarts in a loop.
# If WAIT_FOR is empty, missing or stopped on purpose (exited, created), do not wait.
# After 15 minutes, start anyway.
DIR=$(cd "$(dirname "$0")" && pwd)
[ -f "$DIR/env" ] && . "$DIR/env"
[ -n "${WAIT_FOR:-}" ] || exit 0
for i in $(seq 1 90); do
  s=$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' "$WAIT_FOR" 2>/dev/null) || exit 0
  case "$s" in
    "running healthy"|exited*|created*) exit 0 ;;
  esac
  sleep 10
done
echo "$WAIT_FOR not healthy after 15 min: starting granite-fim anyway"
