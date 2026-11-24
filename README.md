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

- **写入失败即回退**：任一 `cpu.max` 写失败，立即把两组恢复到本次转移前的配额。
- **重启恢复**：启动时回退“未登记成功”的转移（日志 `in_progress`），
  保留“已登记成功”的新配额（日志 `registered`）。
- **恢复完成前拒绝新转移**（HTTP 503），查询接口不受影响。
- **独占管理**：进程内互斥锁 + 状态目录 `flock`，拒绝第二个实例同时管理。

## 目录结构

```
app/
  cgroups.py   # cpu.max 解析/读写（写后 fsync）
  journal.py   # 转移日志（WAL）与原子写 JSON
  service.py   # 核心：转移、失败回退、启动恢复、不变式
  main.py      # FastAPI 入口与依赖装配
cgroup/        # 普通文件模拟的两组 cpu.max（初始 100000/50000，period 100000）
state/         # 运行期状态：transfer_journal.json、last_result.json、service.lock
tests/         # pytest：不变式、失败回退、崩溃恢复、API
examples/demo.sh  # 端到端演示（docker / local 两种模式）
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
| `MIN_QUOTA` | `20000` | 任一组 quota 下限 |
| `CRASH_AFTER_DONOR_WRITE` | `0` | 演示用：置 `1` 模拟“让出方降额后进程崩溃” |

## API 示例

```bash
# 发起划转：groupB 让出 20000 给 groupA
curl -X POST http://127.0.0.1:8000/transfers \
  -H 'Content-Type: application/json' \
  -d '{"donor": "groupB", "amount": 20000}'
# => {"result":"success","receiver":"groupA","post":{"groupA":"120000 100000","groupB":"30000 100000"},...}

# 查询两组配额
curl http://127.0.0.1:8000/quotas
# => {"groupA":{"quota":"120000","period":100000,...},"groupB":{"quota":"30000",...}}

# 查询恢复状态与最近一次处理结果
curl http://127.0.0.1:8000/status
# => {"recovered":true,"min_quota":20000,"last_result":{"action":"transfer","result":"success",...}}
```

错误码：`400` 请求非法（未知组名、低于下限、period 不一致、quota 为 max 等）；
`422` 参数校验失败；`503` 启动恢复未完成；`500` 写文件失败（已回退）。

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
5. 下限校验：让出 15000 使 groupB 低于下限 20000，被 400 拒绝。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

20 个用例覆盖：总额不变式（转移与回退全程逐次校验）、下限边界、写失败回退、
崩溃后重启回退、已登记保留、恢复失败持续拒绝、结果持久化、独占锁、API 各端点。

## 设计要点

- **日志状态机**：`in_progress`（已记意图与转移前配额）→ 写两组 cpu.max →
  `registered`（登记成功）→ 删除日志。崩溃落在 `in_progress` 一律回退；
  落在 `registered` 一律保留。日志与结果文件均“临时文件 + fsync + rename”原子写入。
- **先降后升**：转移先写让出方；回退按 delta 排序先写降额组，
  保证任意中间时刻总额不超过转移前总额。
- **最近一次处理结果**持久化在 `state/last_result.json`，重启后仍可查询。

## 已知限制 / 未完成

- 仅支持数值 quota；任一组为 `max` 时拒绝划转（返回 400）。
- 两组 period 必须相同，否则拒绝划转；服务不修改 period。
- 登记（`registered`）写盘失败的极端情况下，转移已生效但未登记，
  按规则将在下次重启时被回退（与“未登记成功即回退”语义一致）。
- 单实例部署：flock 防止同机多实例，但不支持跨机并发管理同一组文件。
- 本仓库用普通文件模拟 cpu.max；接入真实 cgroup 时只需把
  `GROUP_*_CPU_MAX` 指向 `/sys/fs/cgroup/...` 并保证容器有写权限。
