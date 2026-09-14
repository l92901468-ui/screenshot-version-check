# screenshot-version-check

一个用来**学习「这类网站怎么搭」**的完整示例：一万个用户上传电脑截图，
服务端识别系统版本号，机械核验「问题是否真的被修好了」。

从前端、API、鉴权、对象存储、异步 worker、状态机、缓存、幂等、日志、健康检查，
一路到 CI/CD、容量估算、自动回滚、容器化接管，**每一环都是能跑起来的真代码**，
只有外部依赖（识图模型、计费系统、漏洞扫描器）是模拟的。

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
              └──▶ 模拟识图模型 ──▶ 机械版本核验
```

旁边还挂着一个 `healthd` 守护进程，持续采指标，异常时**自动回滚**。

## 三、目录导览

| 文件 | 职责 |
|---|---|
| `app.py` | API 服务。登录/提交/查状态/健康检查，状态码与动作的完整映射 |
| `worker.py` | 异步任务 worker，多线程并发消费 pending 任务 |
| `state_machine.py` | 状态机。所有状态跳转必须过这张表，非法跳转直接拒绝 |
| `db.py` | 数据层。**带连接池**（`queue.Queue` 实现，可配 `DB_POOL_SIZE`） |
| `cache.py` | TTL 缓存，硬/软 TTL，冲突以 DB 为准 |
| `auth_token.py` | Bearer Token，HMAC 自签发自校验（**无状态的关键**） |
| `validate.py` | 文件合法性校验：大小、格式、魔数 |
| `object_store.py` | 对象存储抽象（key / path / hash），当前落本地盘 |
| `recognize.py` | **模拟**识图模型：出版本号 |
| `billing.py` | **模拟**计费：欠费返回 402 |
| `metrics.py` | 指标采集：通过率、延时 p95/p99、错误率、CPU、内存、队列深度、DLQ |
| `healthd.py` | 健康检查守护进程 + 自动回滚触发 |
| `logutil.py` | 结构化日志，全程记录 |
| `cd/capacity.py` | 容量估算：从实测数据算该开几个 worker、几个线程 |
| `portal/` | 前端。登录 / 上传 / 查状态三个页面，纯静态，由 nginx 托管 |

## 四、几个值得单独说的设计

### 1. 无状态 API

三个 api 实例，杀掉任意一个都不丢状态：

- **Token 自带全部信息**（HMAC-SHA256 签发，验签不需要查库、不需要共享 session）
- **状态只存在 DB 和对象存储**里，进程本地没有业务状态
- **幂等靠 DB 唯一索引**，不是靠进程内去重表
- 进程内的 TTL 缓存只是加速器，可以用 `CACHE_ENABLED=0` 整个关掉，正确性不受影响

`/api/health` 会返回 `instance` 段（实例名、pid、启动时间、缓存开关、连接池水位），
可以直接看出来这是个可随意替换的副本。

### 2. 连接池

SQLite 每次 `connect()` 都要开文件、做 PRAGMA，高并发下很浪费。
`db.py` 里用标准库 `queue.Queue` 实现了一个池：

```python
POOL_SIZE    = int(os.environ.get("DB_POOL_SIZE", "8"))     # 上限
POOL_TIMEOUT = float(os.environ.get("DB_POOL_TIMEOUT", "10"))  # 借不到最多等多久

with POOL.connection() as con:      # 借出 → 用完自动归还
    con.execute("...")
```

池满且超时会抛 `RuntimeError("数据库连接池耗尽")`，
配合 `busy_timeout=30000` 兜住 SQLite 的写锁竞争。
实时水位在 `/api/health` 的 `instance.db_pool` 里能看到。

### 3. 状态码 → 用户动作

| 状态码 | 含义 | 前端动作 |
|---|---|---|
| 200 | 成功 | 显示结果 |
| 400 / 401 / 403 | 请求或身份有问题 | **重新登录** |
| 402 | 欠费 | 提示充值 |
| 404 / 部分 5xx | 服务本身不对劲 | **暂停服务，人工检查** |
| 429 / 其余 5xx | 限流或暂时不可用 | **等一会儿重试**（退避重试，≤3 次） |
| timeout | 超时 | 提示稍后再试，并去查原因 |

重试 3 次仍失败 → 进 **DLQ**，状态变成「待人工审核」。

### 4. CI/CD

```
push → build(Docker) → test(容器内跑单测) → scan(门禁)
                                              ↓
                      image → staging → 健康检查门禁 → prod → 健康检查门禁
                                              ↓ 不达标
                                          自动回滚(git reset + 重启 + 复检)
```

```bash
bash ci/ci.sh          # CI
bash cd/deploy.sh      # CD
bash pipeline.sh       # 一键串起来
```

**模拟的部分**（真实环境换成对应工具即可）：
镜像漏洞扫描（trivy）、依赖成分扫描（SCA）、静态规则扫描（semgrep）、
识图模型、计费系统。

### 5. 容量是怎么算出来的

不是拍脑袋。`cd/capacity.py` 先实测单实例开销，再从两个方向夹：

- **需求侧**：10000 用户 / 60 分钟 = 2.78 task/s，算上重试放大 ×2 = 5.56，
  利用率压在 70% 以下再加 2 倍安全系数 → **需要 8 并发**
- **资源侧**：内存 292 / CPU 112 / SQLite 写入上限 32 → **天花板 32 并发**

取 `min()` → **2 个 worker × 4 线程**。实测占用约 141MB，在 1670MB 预算里，CPU 5%。

### 6. 容器化接管（已写好，未启用）

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
python3 -m unittest discover -s tests -q          # 17 个测试
DB_PATH=./app.db PORT=8001 python3 app.py         # 起 API
WORKER_ID=1 WORKER_THREADS=4 python3 worker.py    # 起 worker
python3 healthd.py                                # 起健康检查守护
```

配置项全部走环境变量：`PORT` `HOST` `DB_PATH` `UPLOAD_DIR` `DB_POOL_SIZE`
`CACHE_ENABLED` `WORKER_ID` `WORKER_THREADS`。

## 六、这是学习用的，不是生产用的

刻意保留的简化：SQLite 而非 PostgreSQL、本地盘而非真 S3/MinIO、
自签证书、单机部署、模拟的模型和计费。
结构上该有的分层、状态机、幂等、可观测性和 CI/CD 都在，
把模拟件换成真的就是另一回事了。
