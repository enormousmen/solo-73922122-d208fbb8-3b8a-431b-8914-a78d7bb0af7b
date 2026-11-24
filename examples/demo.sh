#!/usr/bin/env bash
# 端到端演示：
#   1) 正常划转（让出方降额、接收方升额，总额不变）
#   2) 重启后保留已登记成功的新配额
#   3) 模拟“让出方降额后进程退出”→ 重启恢复、回退未登记成功的转移
#   4) 下限校验（低于设定下限的划转被拒绝）
#
# 用法：
#   examples/demo.sh docker   # 用 Docker Compose 启动（默认）
#   examples/demo.sh local    # 不用 Docker，直接本机跑（需 uvicorn，或用 UVICORN 指定路径）
set -euo pipefail

MODE="${1:-docker}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CGROUP_DIR="$ROOT/cgroup"
STATE_DIR="$ROOT/state"
UVICORN="${UVICORN:-uvicorn}"
SERVER_PID=""

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
show() { printf '  %s\n' "$*"; }

reset_files() {
  mkdir -p "$CGROUP_DIR/groupA" "$CGROUP_DIR/groupB" "$STATE_DIR"
  echo "100000 100000" > "$CGROUP_DIR/groupA/cpu.max"
  echo "50000 100000"  > "$CGROUP_DIR/groupB/cpu.max"
  rm -f "$STATE_DIR"/transfer_journal.json "$STATE_DIR"/last_result.json
}

start_service() {  # $1 = CRASH_AFTER_DONOR_WRITE (0/1)
  if [ "$MODE" = docker ]; then
    CRASH_AFTER_DONOR_WRITE="$1" docker compose -f "$ROOT/docker-compose.yml" up -d --build >/dev/null
  else
    ( cd "$ROOT" && \
      GROUP_A_CPU_MAX="$CGROUP_DIR/groupA/cpu.max" \
      GROUP_B_CPU_MAX="$CGROUP_DIR/groupB/cpu.max" \
      STATE_DIR="$STATE_DIR" \
      MIN_QUOTA=20000 \
      CRASH_AFTER_DONOR_WRITE="$1" \
      exec "$UVICORN" --factory app.main:app_factory --host 127.0.0.1 --port 8000 \
    ) >/tmp/quota-transfer.log 2>&1 &
    SERVER_PID=$!
  fi
  wait_ready
}

stop_service() {
  if [ "$MODE" = docker ]; then
    docker compose -f "$ROOT/docker-compose.yml" down >/dev/null 2>&1 || true
  elif [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      kill -9 "$SERVER_PID" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$SERVER_PID" 2>/dev/null || break
        sleep 0.25
      done
    fi
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "警告: 无法终止服务进程 $SERVER_PID（受限环境？），继续执行" >&2
    else
      wait "$SERVER_PID" 2>/dev/null || true
    fi
    SERVER_PID=""
  fi
}

wait_ready() {
  for _ in $(seq 1 50); do
    if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 0.2
  done
  echo "服务未就绪" >&2; exit 1
}

show_quotas() {
  show "groupA/cpu.max = $(cat "$CGROUP_DIR/groupA/cpu.max")"
  show "groupB/cpu.max = $(cat "$CGROUP_DIR/groupB/cpu.max")"
  curl -fsS "$BASE_URL/quotas"; echo
}

post_transfer() {  # $1 donor, $2 amount；返回 HTTP 状态码
  curl -sS -o /tmp/transfer_resp.json -w '%{http_code}' \
    -X POST "$BASE_URL/transfers" \
    -H 'Content-Type: application/json' \
    -d "{\"donor\": \"$1\", \"amount\": $2}"
}

trap 'stop_service' EXIT

# ---------------------------------------------------------------- 1. 正常划转
say "0. 重置模拟 cpu.max（groupA=100000, groupB=50000, period=100000）并启动服务（$MODE）"
stop_service; reset_files; start_service 0
show_quotas

say "1. 值班员发起划转：groupB 让出 20000 给 groupA"
code=$(post_transfer groupB 20000); show "HTTP $code"; cat /tmp/transfer_resp.json; echo
show_quotas
show "两组之和始终等于转移前总额 150000（先降后升，过程中不超过）"

# ---------------------------------------------------------------- 2. 重启保留
say "2. 重启服务：已登记成功的转移保留新配额"
stop_service; start_service 0
show_quotas
show "最近一次处理结果（重启后仍可查）:"
curl -fsS "$BASE_URL/status"; echo

# ---------------------------------------------------------------- 3. 崩溃恢复
say "3. 模拟故障：让出方降额后进程退出（CRASH_AFTER_DONOR_WRITE=1）"
stop_service; start_service 1
code=$(post_transfer groupB 10000 || true); show "HTTP ${code:-连接被重置}（进程已退出）"
sleep 0.5
show "崩溃现场的两组配额（让出方已降额、接收方未升额）:"
show "groupA/cpu.max = $(cat "$CGROUP_DIR/groupA/cpu.max")"
show "groupB/cpu.max = $(cat "$CGROUP_DIR/groupB/cpu.max")"
show "未登记成功的转移日志:"
cat "$STATE_DIR/transfer_journal.json"; echo

say "4. 重启服务：恢复未完成前拒绝新转移，恢复时回退未登记成功的转移"
stop_service; start_service 0
show_quotas
show "最近一次处理结果:"
curl -fsS "$BASE_URL/status"; echo

# ---------------------------------------------------------------- 5. 下限校验
say "5. 下限校验：groupB 当前 30000，下限 20000，让出 15000 将被拒绝"
code=$(post_transfer groupB 15000); show "HTTP $code"; cat /tmp/transfer_resp.json; echo
show_quotas

say "演示完成"
