#!/usr/bin/env bash
# 普通文件模拟 cgroup v2 cpu.max 的 API 示例。
# 前置：docker compose up -d --build（见 README「启动」），
#       仓库内 cgroup/{groupA,groupB}/cpu.max 即被服务独占管理的“接口文件”，
#       可在宿主机直接 cat 对照磁盘真值（本脚本只通过 HTTP 与服务交互）。
# 环境变量 BASE_URL 可指向任一实例，默认本机 compose 端口。
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
post() {  # $1 = JSON body
  curl -sS -w '\n[HTTP %{http_code}]\n' -X POST "$BASE_URL/transfers" \
    -H 'Content-Type: application/json' -d "$1"
}

say "1. 查询实际周期与配额（/quotas 返回磁盘真值；periods 给出基线/当前周期）"
curl -sS "$BASE_URL/quotas"; echo

say "2. 按基线周期单位划转：groupB 让出 20000（微秒/基线周期）给 groupA"
post '{"donor": "groupB", "amount": 20000}'

say "3. 切到短周期（period=50000）后，amount 的语义不变"
say "   切换是启动配置：CPU_PERIOD_USEC=50000 docker compose up -d --build"
say "   切换成功时两组 quota 同比减半（100000→50000、50000→25000），CPU 占比不变；"
say "   之后划转 amount=2000（基线单位），实际写入 applied_delta=1000（2000 × 50000/100000）。"
post '{"donor": "groupB", "amount": 2000}'

say "4. 无法精确换算成整数的划转量被拒（period=50000 时 amount=101 → 50.5）"
post '{"donor": "groupB", "amount": 101}' || true

say "5. /status 显示周期切换结果与最近一次处理"
curl -sS "$BASE_URL/status"; echo
