# CPU 配额划转服务（cgroup v2 cpu.max）

同一台构建机上，一组 CI 执行器忙不过来时，值班员通过本服务把另一组的部分
CPU 配额划给它。服务独占管理两组的 cgroup v2 `cpu.max`（本仓库用普通文件模拟，
接口与 `/sys/fs/cgroup/<group>/cpu.max` 完全一致），并保证转移/回退全程的
配额不变式。

## 保证的不变式

转移或回退的任意中间时刻：

1. **两组 quota 之和不超过本次转移前的总额** —— 转移本身保持总额不变
   （让出方减多少、接收方加多少），且所有两组写入都“先降后升”：
   正常转移先写让出方（降）再写接收方（升）；回退先写需要降的组再写需要升的组。
2. **任一组 quota 不低于设定下限**（`MIN_QUOTA`）—— 划转前校验让出方
   划转后的值；回退目标即转移前的合法值。

其他规则：

- **写入失败即回退**：任一 `cpu.max` 写失败，立即把两组恢复到本次操作前的配额。
- **重启恢复**：启动时回退“未登记成功”的转移/周期切换（日志 `in_progress`），
  保留“已登记成功”的新配额（日志 `registered`）。
- **恢复完成前拒绝新转移**（HTTP 503），查询接口不受影响。
- **独占管理**：进程内互斥锁 + 状态目录 `flock`，拒绝第二个实例同时管理。

## 可选：短周期切换（`CPU_PERIOD_USEC`）

构建进程用完当期 CPU 配额后只能等下一轮。服务支持可选启动配置，把两组
`cpu.max` 一次性切到指定的**较短** period（如 `100000 → 50000`）：

- **同比缩放、各自原值不变**：切换与回退中，每次成功写入后，每个组的
  `quota/period` 比值都保持切换前的值（`100000/100000 == 50000/50000`），
  即 CPU 时间占比不变，只是周期变短、配额随周期同比缩小。
- **换算不到整数即拒绝切换**：任一组 `quota × 新period / 旧period` 不整除时，
  整体拒绝，两组文件一个字节都不动，继续按旧周期服务。
- **单次写入失败即还原两组**：切换中任一 `cpu.max` 写失败，立即把两组恢复到
  旧周期的原值，本次启动按旧周期服务。
- **崩溃语义与转移一致**：切换也走 WAL（`in_progress → registered`）。进程中途
  退出后重启，未登记的切换被**撤回**（还原两组、本次启动按旧周期且不自动重试）；
  已登记的切换**保留**。
- **只切一次**：整个生命周期至多成功切换一次；成功后即使重启仍带该配置，
  也不会重复切换。
- **核算单位锚定首次切换前的周期（基线周期）**：`/transfers` 的 `amount` 与
  `MIN_QUOTA` 始终按基线周期核算，基线持久化在 `state/period_state.json`，
  重启不变。服务把 `amount` 精确换算到当前周期后再划转；换算不到整数时拒单。
  例如基线 `100000`、当前 `50000` 时，`amount=20000` 实际写入 delta `10000`，
  而 `amount=101`（→50.5）会被拒绝。

## 目录结构

```
app/
  cgroups.py   # cpu.max 解析/读写（写后 fsync）
  journal.py   # 转移/切换日志（WAL）与原子写 JSON
  service.py   # 核心：转移、周期切换、失败回退、启动恢复、不变式
  main.py      # FastAPI 入口与依赖装配
cgroup/        # 普通文件模拟的两组 cpu.max（初始 100000/50000，period 100000）
state/         # 运行期状态：transfer_journal.json、last_result.json、
               # period_state.json（基线/当前周期）、service.lock
tests/         # pytest：不变式、失败回退、崩溃恢复、周期切换、API
examples/demo.sh          # 端到端演示（docker / local 两种模式）
examples/api-examples.sh  # 普通文件模拟下的 API 调用示例
```

## 启动

```bash
docker compose up -d --build
```

环境变量（compose 中已配好默认值）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GROUP_A_CPU_MAX` / `GROUP_B_CPU_MAX` | `/cgroup/groupA/cpu.max` … | 两组 cpu.max 路径 |
| `STATE_DIR` | `/state` | 日志与结果目录 |
| `MIN_QUOTA` | `20000` | 任一组 quota 下限（按**基线周期**核算） |
| `CPU_PERIOD_USEC` | 空 | 可选：启动时把两组切到该较短 period（微秒）；留空不切换 |
| `CRASH_AFTER_DONOR_WRITE` | `0` | 演示用：置 `1` 模拟“首个组写入后进程崩溃”（转移/切换各对应一个阶段） |

启用短周期切换（compose 会透传该变量）：

```bash
CPU_PERIOD_USEC=50000 docker compose up -d --build
```

## API 示例

```bash
# 发起划转：groupB 让出 20000（始终按基线周期核算的单位）给 groupA
curl -X POST http://127.0.0.1:8000/transfers \
  -H 'Content-Type: application/json' \
  -d '{"donor": "groupB", "amount": 20000}'
# 短周期（50000）下：amount=20000 仍是基线单位，applied_delta=10000
# => {"result":"success",...,"amount":20000,"amount_period":100000,
#     "applied_delta":10000,"active_period":50000,
#     "post":{"groupA":"60000 50000","groupB":"15000 50000"},...}

# 查询两组配额与实际周期（periods 同时给出基线/当前周期）
curl http://127.0.0.1:8000/quotas
# => {"groupA":{"quota":"60000","period":50000,...},"groupB":{...},
#     "periods":{"baseline_period":100000,"active_period":50000}}

# 查询恢复状态、周期切换结果与最近一次处理结果
curl http://127.0.0.1:8000/status
# => {"recovered":true,"min_quota":20000,"min_quota_period":100000,
#     "period_switch":{"configured_target_period":50000,"baseline_period":100000,
#       "active_period":50000,"switched":true,"suppressed_after_rollback":false},
#     "last_result":{"action":"period_switch","result":"switched",...}}
```

更多可直接运行的示例见 `examples/api-examples.sh`（普通文件模拟，仅通过 HTTP 交互）。

错误码：`400` 请求非法（未知组名、低于下限、period 不一致、quota 为 max、
划转量/配额无法精确换算为整数等）；`422` 参数校验失败；
`503` 启动恢复未完成；`500` 写文件失败（已回退）。

## 演示（让出方降额后进程退出 → 重启恢复）

```bash
examples/demo.sh docker   # 或 examples/demo.sh local（本机直接跑 uvicorn）
```

演示流程：

1. 正常划转 `groupB --20000--> groupA`：两组变为 `120000 / 30000`，总额恒为 150000；
2. 重启服务：已登记成功的转移**保留**新配额；
3. `CRASH_AFTER_DONOR_WRITE=1` 时发起划转：让出方降额后进程退出，
   现场为 `groupB=20000`（已降）、`groupA=120000`（未升）、日志 `in_progress`；
4. 正常重启：启动恢复**回退**未登记成功的转移，`groupB` 恢复为 `30000`，
   `/status` 显示 `rolled_back`；
5. 下限校验：让出 15000 使 groupB 低于下限 20000，被 400 拒绝；
6. `CPU_PERIOD_USEC=50000` 启动：两组同比缩放到 `50000 / 25000 @50000`，
   基线周期 100000 持久化；切换后按原单位划转（`amount=20000 → delta=10000`）、
   非整数拒单（`amount=101`）、下限仍按基线核算；
7. 切换途中进程退出：重启后**撤回**未登记切换、两组还原旧周期，
   本次启动不再自动重试（`/status` 的 `suppressed_after_rollback=true`）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

42 个用例覆盖：总额不变式（转移与回退全程逐次校验）、下限边界、写失败回退、
崩溃后重启回退、已登记保留、恢复失败持续拒绝、结果持久化、独占锁、API 各端点；
以及周期切换的：启动同比缩放、逐次写入比值不变、非整数拒绝、目标不更短拒绝、
写失败还原、崩溃撤回与不重试、已登记保留、无配置重启保留、基线单位划转、
下限按基线核算、非整数划转拒单、重启后核算单位不变。

## 设计要点

- **日志状态机**：`in_progress`（已记意图与操作前配额）→ 写两组 cpu.max →
  `registered`（登记成功）→ 删除日志。崩溃落在 `in_progress` 一律回退；
  落在 `registered` 一律保留。日志、结果与周期状态文件均
  “临时文件 + fsync + rename”原子写入。日志条目带 `action` 字段区分
  `transfer` 与 `period_switch`，恢复路径据此给出对应结果。
- **先降后升**：转移先写让出方；回退按 delta 排序先写降额组，
  保证任意中间时刻总额不超过操作前总额。
- **周期切换 = 同比缩放**：切换前先对两组做完整的整除预校验，任一不整除则
  整体拒绝、不触碰文件；切换与回退的每个写入点上，各组 `quota/period` 比值
  都等于切换前原值。
- **核算单位与周期解耦**：`state/period_state.json` 记录
  `baseline_period`（首次切换前周期）/`active_period`/`switched`。
  `amount`、`MIN_QUOTA` 只认基线周期，划转时做精确整数换算，重启后语义不变。
- **撤回不重试仅限当次启动**：重启撤回未登记切换后置进程内标记
  `suppressed_after_rollback`，本次启动按旧周期服务；下次全新启动配置仍在时
  可重新切换（标记不持久化）。
- **最近一次处理结果**持久化在 `state/last_result.json`，重启后仍可查询。

## 已知限制 / 未完成

- 仅支持数值 quota；任一组为 `max` 时拒绝划转与周期切换（返回 400）。
- 两组 period 必须相同，否则拒绝划转；周期切换只接受**短于**当前 period 的目标，
  且整个生命周期至多成功切换一次（不支持运行期再切/回退到更长周期）。
- 登记（`registered`）写盘失败的极端情况下，操作已生效但未登记，
  按规则将在下次重启时被回退（与“未登记成功即回退”语义一致）。
- 单实例部署：flock 防止同机多实例，但不支持跨机并发管理同一组文件。
- 本仓库用普通文件模拟 cpu.max；接入真实 cgroup 时只需把
  `GROUP_*_CPU_MAX` 指向 `/sys/fs/cgroup/...` 并保证容器有写权限。
  真实 cgroup 对 period/quota 还有取值范围与粒度约束（如 period 典型
  1000~1000000 微秒），需在部署时自行保证目标值合法。
- 本环境未安装 Docker，`docker compose` 路径仅做了 compose 配置与脚本语法核对，
  端到端实测在本机以 uvicorn（local 模式）完成。
