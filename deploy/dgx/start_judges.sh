#!/usr/bin/env bash
# Start everything in one tmux session: judge_a, judge_b, embedding, agent-under-test (vLLM) and the SparkJury API.
# All vLLM servers bind 127.0.0.1; only the API binds 0.0.0.0:$API_PORT (public 9030) and it requires SPARKJURY_API_TOKEN.
# Usage: bash deploy/dgx/start_judges.sh [--no-agent] [--no-api]
cd "$(dirname "$0")/../.." && source deploy/dgx/common.sh
NO_AGENT=0; NO_API=0
for a in "$@"; do case "$a" in --no-agent) NO_AGENT=1 ;; --no-api) NO_API=1 ;; esac; done
# API 窗口跑的是 `sparkjury serve --host 0.0.0.0`，而 serve 在没 token 时会拒绝启动
# （「8888 和 9000 上对外提供的服务必须有鉴权」是节点手册红线，日志里滚过去的一句警告拦不住人）。
# 在这里就拦掉，别等五个窗口都起来了才发现 API 那个窗口是死的。token 来自 deploy/dgx/.env，
# common.sh 已经 source 过了，口径和 API 窗口里那次 source 一致。
if [[ $NO_API -eq 0 && -z "${SPARKJURY_API_TOKEN:-}" ]]; then
  die "SPARKJURY_API_TOKEN 是空的：公网 cockpit 必须有鉴权。把它写进 deploy/dgx/.env（参照 env.example），或加 --no-api 只起裁判。"
fi
[[ -x "$VLLM_BIN/vllm" ]] || die "vLLM not found at $VLLM_BIN; run deploy/dgx/setup_node.sh"
log "using vLLM at $VLLM_BIN ($("$VLLM_BIN/python" -c "import vllm;print(vllm.__version__)" 2>/dev/null))"
mkdir -p logs

if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  warn "tmux session '$TMUX_SESSION' already exists; run deploy/dgx/stop_all.sh first or attach: tmux attach -t $TMUX_SESSION"
  exit 1
fi
tmux new-session -d -s "$TMUX_SESSION" -n shell "cd '$REPO_ROOT'; bash"
loginctl enable-linger "$USER" 2>/dev/null || true   # keep our processes alive when the last SSH session closes

# Qwen3 chat template: disable thinking for judges (shorter, deterministic JSON)
QWEN_ARGS="--max-model-len 32768 --enable-auto-tool-choice --tool-call-parser hermes"   # tau2 retail prompts exceed 8k; the user simulator needs 32k
vllm_window judge_a "$JUDGE_A_MODEL" "$JUDGE_A_PORT" "$JUDGE_A_MEM" $QWEN_ARGS
vllm_window judge_b "$JUDGE_B_MODEL" "$JUDGE_B_PORT" "$JUDGE_B_MEM" --max-model-len 16384 --enable-auto-tool-choice --tool-call-parser hermes
vllm_window embed   "$EMBED_MODEL"   "$EMBED_PORT"   "$EMBED_MEM"   --runner pooling --convert embed --max-model-len 4096 --enforce-eager --kv-cache-memory-bytes 1073741824
if [[ $NO_AGENT -eq 0 ]]; then
  vllm_window agent "$AGENT_MODEL" "$AGENT_PORT" "$AGENT_MEM" $QWEN_ARGS --kv-cache-memory-bytes 8589934592   # explicit KV budget: bypasses the free-memory check on unified memory
fi

if [[ $NO_API -eq 0 ]]; then
  log "window api: token 已配（长度 ${#SPARKJURY_API_TOKEN}），serve 会带鉴权启动"
  tmux new-window -t "$TMUX_SESSION" -n api \
    "cd '$REPO_ROOT' && set -a && source deploy/dgx/.env && set +a && uv run sparkjury serve --host 0.0.0.0 --port $API_PORT 2>&1 | tee -a logs/api.log; read"
  log "window api: cockpit on 0.0.0.0:$API_PORT (public http://61.172.235.130:9030/?token=...)"
fi

log "waiting for the judges to answer /models (model load can take minutes)..."
for spec in "judge_a:$JUDGE_A_PORT" "judge_b:$JUDGE_B_PORT" "embed:$EMBED_PORT"; do
  name="${spec%%:*}"; port="${spec##*:}"
  if wait_http "http://127.0.0.1:$port/v1/models" 900; then log "$name ready on $port"; else warn "$name not ready after 15 min: tmux attach -t $TMUX_SESSION, window $name"; fi
done
bash deploy/dgx/status.sh
log "attach with: tmux attach -t $TMUX_SESSION   (Ctrl+B then D to detach)"
