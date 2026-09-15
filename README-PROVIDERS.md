# Recognition provider modes

这个 demo 现在保留两条识图路径，用来区分“内部模型”和“第三方 API”在工程边界上的差异。

## 1. Internal model

```text
worker
  ↓
internal vision model
  ↓
version extraction
  ↓
fenced finalize
```

配置：

```bash
VISION_BACKEND=internal
```

特点：

- 不需要外部 API token；
- 不依赖第三方 provider availability；
- demo 不对内部模型做 per-call 扣费；
- worker 的 lease / generation / retry / DLQ 语义保持不变。

## 2. External API

```text
worker
  ↓
provider_control (shared state)
  ↓ enabled + token active?
Bearer token / credential generation
  ↓
external vision API (simulated)
  ↓
version extraction
  ↓
fenced finalize + simulated usage charge
```

配置：

```bash
VISION_BACKEND=external
EXTERNAL_API_TOKEN=demo-secret
EXTERNAL_API_CREDENTIAL_VERSION=1
CONTROL_BLOCK_RETRY_SEC=30
```

`EXTERNAL_API_TOKEN` 只从环境变量读取，代码不会把 token 值写入 DB、日志或 incident evidence。
真实生产应替换成 secret manager / workload identity，并通过 HTTPS/mTLS 等受控通道调用供应商。

外部模式保留 `billing.py` 的原因是用它模拟“第三方 API 调用额度/成本”，而不是模拟员工真的拿自己的钱付截图识别费。并发扣费的正确性仍要由数据库里的条件更新保证，不能只靠 worker 先读余额。

外部 provider 被安全控制面暂停、token 未部署、或 worker 仍持有旧 credential generation 时，这些并不是“截图识别失败”。worker 会把任务重新放回 `pending`，等待 `CONTROL_BLOCK_RETRY_SEC` 后再检查，并且**不增加业务 `retry_count`**。这样一次 token incident 不会把正常任务的三次识别重试全部烧完后送进 DLQ。是否切到 internal backend 由运维/策略显式决定，不做静默自动 fallback。

## External token exposure workflow

第三方路径额外有一个 token 泄露场景。核心原则是：**containment 不等待完整 scope，但 destructive remediation / close 需要人工批准与供应商确认。**

```text
Detect suspected token exposure
        ↓
Capture minimal evidence snapshot
(no secret value)
        ↓
Revoke / rotate credential generation
+ pause external provider
        ↓
Initial escalation
(scope still under investigation)
        ↓
Scope investigation
(request ids / time window / data categories)
        ↓
Vendor containment request
(freeze / quarantine suspected window)
        ↓
Vendor acknowledgement
        ↓
Human approval
        ↓
Targeted remediation / deletion
        ↓
Vendor confirmation
        ↓
Deploy rotated credential
        ↓
Resume provider
        ↓
Close + audit evidence
```

本 demo 用 `provider_control.py` 持久化共享 provider 状态、当前 active incident id 和 append-only incident events；`token_incident.py` 提供可运行的模拟命令。开始 containment 后，同一个 provider 不允许再开启第二个重叠 incident，避免审计事件串错。

示例：

```bash
python3 token_incident.py start --reason "token pasted into public log" --suspected-since "2026-09-15T09:00:00Z"
# 记下输出的 INC-xxxx
python3 token_incident.py scope --incident INC-xxxx --detail "requests 1201-1249; screenshots only"
python3 token_incident.py vendor --incident INC-xxxx --detail "vendor acknowledged quarantine"
python3 token_incident.py close --incident INC-xxxx --approve --vendor-confirmed --credential-deployed
python3 token_incident.py show --incident INC-xxxx
```

### 为什么 provider pause 要放进共享 DB？

如果只在某个 worker 内存里设 `paused=True`，其他 worker 仍可能继续用泄露 token 调供应商，这又把系统变成有本地权威状态。共享 provider control 让所有 worker 看到同一 containment 状态。

### 为什么 pause 期间不消耗普通 retry？

`retry_count` 表示的是“这个截图的识别尝试失败了几次”。而 token 被撤销、供应商被主动暂停属于控制面状态，不说明截图本身难以识别。把两种失败混在一起，会导致 incident containment 期间大量任务无意义进入 DLQ。因此控制面阻断采用独立等待窗口，业务 retry 保持不变。

### 为什么 rotate 以后还要显式确认 credential 已部署？

“把 credential generation +1”只是本 demo 的控制面模拟，不等于真实新 secret 已经送到所有 worker。恢复 provider 之前必须确认 rotated credential 已通过 secret manager / 受控渠道部署完成；否则一恢复流量，旧 worker 只会拿旧 token 打出一片 `401`。

### 什么由自动化做，什么留给人？

自动化适合：检测后创建 incident、保存最小证据、立即 revoke/rotate、暂停 provider、初始告警、收集 request IDs / 时间窗、生成 vendor containment request、跟踪 acknowledgement。

人工边界：最终 scope 判断、不可逆删除、供应商数据处置确认、确认新 credential 已部署、incident close。这样既不因为“等人批准”而延误 credential containment，也不让 Agent/脚本直接做不可逆高风险动作。
