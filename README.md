# screenshot-version-check

一个用来**学习「这类网站怎么搭」**的完整示例：一万个用户上传电脑截图，
服务端识别系统版本号，机械核验「问题是否真的被修好了」。

从前端、API、鉴权、对象存储、异步 worker、状态机、缓存、幂等、日志、健康检查，
一路到 CI/CD、容量估算、自动回滚、容器化接管，**每一环都是能跑起来的真代码**，
只有外部依赖（识图模型、第三方 API、漏洞扫描器）是模拟的。

识图现在有两条路径：`VISION_BACKEND=internal` 模拟公司内部模型；
`VISION_BACKEND=external` 模拟第三方 API，并额外展示 token/credential、调用额度和供应商事件响应边界。

在线地址：`https://114.132.222.64:8443/`（自签证书，浏览器会告警，选「继续访问」）
演示账号：`demo` / `demo123`

---

## 一、它能干什么

```
登录 → 选截图 → 确认提交 → 看到「等待中」
                              ↓ worker 异步识别
        ┌─────────────────────┼─────────────────────┐
        ↓                     ↓                     ↓
   提交成功                异常需重传           待人工审核(DLQ)
  （版本已达标）        （用户再传一次）      （重试 3 次仍失败）
```

## 二、整体架构

```
                        ┌──────────────┐
   用户 ──HTTPS:8443──▶ │    nginx     │  静态托管 + TLS 终结 + 负载均衡(least_conn)
                        └──────┬───────┘
                 ┌────────────┼────────────┐
                 ↓            ↓            ↓
            api:8001     api:8002     api:8003      无状态，可任意增删
                 └────────────┼────────────┘
                              ↓
              ┌───────────────┴───────────────┐
              ↓                               ↓
        SQLite(WAL + 连接池)            TTL 缓存(只读加速)
              ↑
       worker@1 / worker@2（各 4 线程）──▶ 对象存储(uploads/)
              │
              ├── internal → 内部识图模型（模拟）
              │
              └── external → provider control → 第三方识图 API（模拟）
                                      │
                                      └── token incident / audit events

两条识图路径最后都回到：版本机械核验 → fenced finalize。
```

旁边还挂着一个 `healthd` 守护进程，持续采指标，异常时**自动回滚**。

## 三、目录导览

| 文件 | 职责 |
|---|---|
| `app.py` | API 服务。登录/提交/查状态/健康检查，上传入口限流 |
| `worker.py` | 异步任务 worker，多线程并发消费 pending 任务 |
| `state_machine.py` | 状态机。所有状态跳转必须过这张表，非法跳转直接拒绝 |
| `db.py` | 数据层。**带连接池**（`queue.Queue` 实现，可配 `DB_POOL_SIZE`） |
| `cache.py` | TTL 缓存，硬/软 TTL，冲突以 DB 为准 |
| `auth_token.py` | Bearer Token，HMAC 自签发自校验（**无状态的关键**） |
| `validate.py` | 文件合法性校验：大小、格式、魔数；分块计算 SHA-256 |
| `object_store.py` | 对象存储抽象；稳定 key、分块写、临时文件 + atomic replace、stale tmp GC |
| `recognize.py` | internal / external 两种识图 backend 的统一入口 |
| `external_provider.py` | **模拟**第三方识图 API：token、credential generation、timeout/5xx |
| `provider_control.py` | 第三方 provider 的共享暂停/credential 状态 + incident audit events |
| `token_incident.py` | **可运行**的外部 API token 泄露 containment / vendor / close 流程 |
| `billing.py` | **模拟**第三方 API 调用 credits；internal backend 不使用 |
| `README-PROVIDERS.md` | 两种 backend 和 token incident 的详细图解 |
| `metrics.py` | 指标采集：通过率、延时 p95/p99、错误率、CPU、内存、队列深度、DLQ |
| `healthd.py` | 健康检查守护进程 + 自动回滚触发 |
| `logutil.py` | 结构化日志，全程记录 |
| `cd/capacity.py` | 容量估算：从实测数据算该开几个 worker、几个线程 |
| `portal/` | 前端。登录 / 上传 / 查状态三个页面，纯静态，由 nginx 托管 |

## 四、几个值得单独说的设计

### 1. 无状态 API + crash-safe 幂等

三个 api 实例，杀掉任意一个都不靠实例内 session 保存业务状态：

- **Token 自带全部信息**（HMAC-SHA256 签发，验签不需要共享 session）
- **状态只存在 DB 和对象存储**里，进程本地没有权威业务状态
- `(user_id, idempotency_key)` 由 DB unique constraint 做最终并发裁决
- 上传前先创建 `status=uploading` 的持久化 reservation，再做对象存储副作用
- 同一个 submission 使用稳定 object key；retry 不会每次生成新的 UUID object
- `file_hash` 是请求 fingerprint；同 key + 不同文件返回 `409 Conflict`
- 进程内 TTL 缓存只是加速器，可以用 `CACHE_ENABLED=0` 关掉，正确性不受影响

DB 和对象存储不是一个 ACID transaction，所以这里不假装存在跨系统原子性；
而是把 `uploading` 显式建模成可恢复中间态，并通过稳定 object key、lease 和重试恢复来收敛失败。

### 2. 上传路径：bounded memory + admission control

旧版本在 `/api/submit` 中直接 `item.file.read()`，每个活跃请求都可能把接近 10MB 的截图
完整复制进 Python 内存。集中上传时，API 可能比异步 worker 更早因为内存耗尽而失败。

现在上传分两遍处理，但两遍都只保留固定大小 chunk：

```
FieldStorage 临时文件
        ↓
第一遍：chunked scan
size + magic + SHA-256
        ↓ rewind
DB reserve: uploading
        ↓
第二遍：chunked object write
        ↓
temp file → fsync → hash check → atomic replace
        ↓
uploading → pending
```

默认 `UPLOAD_CHUNK_SIZE=1MiB`。因此主上传代码不再随单个文件大小线性增加 Python `bytes` 内存。

Streaming 只解决**单请求内存放大**，不代表可以免费接受无限并发。因此每个 API 实例还有一个
`MAX_INFLIGHT_UPLOADS`（默认 16）的 non-blocking semaphore：没有 slot 时直接返回 `429` + `Retry-After`，
而不是让所有请求一起进入 multipart 解析 / hash / object write 路径。

这个 admission control 是 demo 级背压；真实生产环境还会在 nginx / API gateway / production WSGI-ASGI server
等层面设置连接、body size、request rate 和资源限制。

### 3. Worker ownership：claim + lease + fencing

worker 不再先 `SELECT pending` 再“假装领取成功”，而是由数据库条件 UPDATE 决定 ownership：

```
pending
  ↓ atomic claim
processing(owner=A, generation=5, lease=T)
  ↓ heartbeat
A crash / lease expire
  ↓
B reclaim → generation=6
```

所有 `processing` 出边都带 owner + generation + lease fencing。旧 worker 即使“回魂”，
也不能覆盖新 owner 的结果；成功结果与本地模拟扣费在同一个 SQLite transaction 里提交，
因此 stale worker 也不能重复扣费。

heartbeat 的数据库异常只代表 `ownership uncertain`，不是立即判定 `lost`；最终仍由 fenced DB write 裁决。

### 4. Internal model vs external API

`recognize.py` 对 worker 暴露同一个调用接口，但两种 backend 的工程边界不同：

```text
internal:
worker → internal model → fenced finalize(cost=0)

external:
worker → shared provider control → token/credential generation → vendor API
       → fenced finalize + simulated API credits
```

外部 credits 只是模拟第三方调用成本/额度，不是“员工自己花钱”。正确性不依赖 worker 先查余额：
最终 SQL 使用 `UPDATE ... WHERE balance >= cost`，并且数据库 trigger 保证 `balance >= 0` invariant。
两个不同任务即使同时看到最后 1 个 credit，也只能有一个原子扣减成功。

第三方 token 泄露则走另一条 GRC workflow：

```text
Detect
  ↓
minimal evidence snapshot (不记录 secret)
  ↓
revoke / rotate + pause provider
  ↓
initial escalation
  ↓
scope investigation + vendor containment
  ↓
human approval
  ↓
targeted remediation + vendor confirmation
  ↓
resume rotated credential
  ↓
close + audit evidence
```

这里特意不是“先调查清楚再 revoke”：credential containment 要快；但不可逆的数据删除和最终 close
保留 human approval / vendor confirmation。详细说明和命令见 `README-PROVIDERS.md`。

### 5. 连接池

SQLite 每次 `connect()` 都要开文件、做 PRAGMA，高并发下很浪费。
`db.py` 里用标准库 `queue.Queue` 实现了一个池：

```python
POOL_SIZE    = int(os.environ.get("DB_POOL_SIZE", "8"))
POOL_TIMEOUT = float(os.environ.get("DB_POOL_TIMEOUT", "10"))

with POOL.connection() as con:
    con.execute("...")
```

池满且超时会抛 `RuntimeError("数据库连接池耗尽")`，
配合 `busy_timeout=30000` 兜住 SQLite 的写锁竞争。
实时水位在 `/api/health` 的 `instance.db_pool` 里能看到。

### 6. 状态码 → 用户动作

| 状态码 | 含义 | 前端动作 |
|---|---|---|
| 200 | 成功 | 显示结果 |
| 400 / 401 / 403 | 请求或身份有问题 | **重新登录** |
| 402 | 外部 API 调用额度不足（external 模式） | 等待额度恢复，或运维切换 internal backend |
| 409 | 同一个 Idempotency-Key 被用于不同请求 | 生成新的 key |
| 429 / 部分 5xx | 繁忙或暂时不可用 | **等一会儿重试** |
| 404 / 其余 5xx | 服务本身不对劲 | **暂停服务，人工检查** |
| timeout | 超时 | 提示稍后再试，并去查原因 |

识图重试 3 次仍失败 → 进 **DLQ**，状态变成「待人工审核」。

### 7. CI/CD

```
push → build(Docker) → test(容器内跑单测) → scan(门禁)
                                              ↓
                      image → staging → 健康检查门禁 → prod → 健康检查门禁
                                              ↓ 不达标
                                          自动回滚(git reset + 重启 + 复检)
```

```bash
bash ci/ci.sh
bash cd/deploy.sh
bash pipeline.sh
```

**模拟的部分**（真实环境换成对应工具即可）：
镜像漏洞扫描（trivy）、依赖成分扫描（SCA）、静态规则扫描（semgrep）、
内部识图模型、第三方识图 API、第三方调用 credits。

### 8. 容量是怎么算出来的

`cd/capacity.py` 先实测单实例开销，再从需求侧和资源侧夹出 worker 并发上限：

- **需求侧**：10000 用户 / 60 分钟 = 2.78 task/s，算上重试放大 ×2 = 5.56，
  利用率压在 70% 以下再加 2 倍安全系数 → **需要 8 worker 并发**
- **资源侧**：内存 292 / CPU 112 / SQLite 写入上限 32 → **天花板 32 worker 并发**

取 `min()` → **2 个 worker × 4 线程**。

注意：worker capacity 和 upload admission 是两个不同容量问题。worker 并发决定异步识图吞吐；
`MAX_INFLIGHT_UPLOADS` + chunk size 控制 API 上传入口的瞬时资源占用。

### 9. 容器化接管（已写好，未启用）

`docker-compose.prod.yml` + `cd/prod-container/{enable,disable}.sh`。
设计上刻意做成**渐进接管、随时可退**：容器监听 911x 避开现网 800x，
数据用 bind mount 让两套栈共享同一个 SQLite 文件，切换只是改 nginx upstream 一行。

没启用的原因写在 `cd/prod-container/README.md` 里，主要是**回滚语义会变**
（现在是 git 回滚，容器化后应该是指回上一个镜像 tag），`rollback.py` 得跟着改。

### 关于 `portal/`

nginx 实际托管的是 `/home/ubuntu/screenshot-portal/`，`portal/` 是它的一份拷贝，
进仓库只是为了让这个项目自包含。**改前端请改 nginx 那份**，改完同步过来。

## 五、跑起来

```bash
# 依赖：Python 3.12 标准库，无第三方包
python3 -m unittest discover -s tests -q
DB_PATH=./app.db PORT=8001 python3 app.py
VISION_BACKEND=internal WORKER_ID=1 WORKER_THREADS=4 python3 worker.py
python3 healthd.py
```

外部 API 模拟：

```bash
VISION_BACKEND=external \
EXTERNAL_API_TOKEN=demo-secret \
EXTERNAL_API_CREDENTIAL_VERSION=1 \
WORKER_ID=1 python3 worker.py
```

Token incident 模拟：

```bash
python3 token_incident.py start --reason "token pasted into public log"
# 然后按输出 incident id 做 scope / vendor / close；完整命令见 README-PROVIDERS.md
```

常用配置项：

- `PORT` `HOST` `DB_PATH` `UPLOAD_DIR`
- `DB_POOL_SIZE` `CACHE_ENABLED`
- `WORKER_ID` `WORKER_THREADS`
- `VISION_BACKEND=internal|external`
- `EXTERNAL_API_TOKEN` `EXTERNAL_API_CREDENTIAL_VERSION`：仅 external 模式
- `UPLOAD_CHUNK_SIZE`：上传扫描 / 对象写入的 chunk 大小，默认 1MiB
- `MAX_INFLIGHT_UPLOADS`：每个 API 实例允许同时进入上传处理路径的请求数，默认 16
- `UPLOAD_LEASE_SEC`：中断上传的恢复 lease
- `PROCESSING_LEASE_SEC` `PROCESSING_HEARTBEAT_SEC`：worker ownership lease
- `TEMP_FILE_MAX_AGE_SEC` `TEMP_GC_INTERVAL_SEC`：stale `.tmp` GC

## 六、这是学习用的，不是生产用的

刻意保留的简化：SQLite 而非 PostgreSQL、本地盘而非真 S3/MinIO、
自签证书、单机部署、`ThreadingHTTPServer` + 简单 semaphore admission control、模拟模型和第三方 API。

结构上重点展示的是：状态机、幂等、失败恢复、bounded-memory upload、backpressure、
worker fencing、provider incident containment、可观测性和 CI/CD。真实生产环境会把这些语义迁移到
PostgreSQL、对象存储、消息队列、真实 IAM/secret manager/provider API、生产级 API server / gateway
和集中监控体系，而不是简单“把模拟件换成真的”就结束。
