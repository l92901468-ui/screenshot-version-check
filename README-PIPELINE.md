# CI/CD 流水线说明

一条命令跑完全流程：

```bash
bash pipeline.sh                    # CI → 部署到 staging（默认，安全）
bash pipeline.sh --prod             # CI → staging → prod
bash pipeline.sh --prod --capacity  # 额外按资源预算调整 worker 并发
bash pipeline.sh --skip-ci          # 跳过 CI，用上次产物直接部署
```

## 全链路

```
开发者改代码
   │
   ├─ 1. push      ci/ci.sh         提交并推到本地裸仓库（模拟 GitLab/GitHub）
   ├─ 2. build     Dockerfile       构建 screenshot-api:<时间戳> 镜像 + 产物校验
   ├─ 3. test      tests/           容器内跑单元测试
   ├─ 4. scan      ci/scan.py       语法/规则/敏感信息（真做）+ 镜像漏洞/SCA（模拟）→ 门禁
   │        │
   │        └─→ ci/reports/last_build.json   {"commit": "...", "image": "..."}
   │                     │
   │                     ↓  CD 读这个交接文件
   ├─ 5. staging   cd/deploy.sh     用上面那个 image 起容器（9001）
   ├─ 6. gate      cd/health_gate.py  连续 N 次探测 /api/health，不通过就停在这里
   │                     │
   │                     ↓  staging 通过才允许
   ├─ 7. capacity  cd/capacity.py   按资源预算决定 worker 实例数 × 线程数并应用
   ├─ 8. prod      cd/deploy.sh     切到同一 commit + 重启 systemd 服务
   ├─ 9. gate      cd/health_gate.py  同一套判据再验一次
   └─ 10. rollback rollback.py      不健康 → 看任务看状态 → 回滚到上一个 good 版本 → 复检
```

## 各环节怎么关联的（不是各写各的）

| 交接物 | 谁写 | 谁读 | 作用 |
|---|---|---|---|
| `ci/reports/last_build.json` | CI（ci.sh） | CD（deploy.sh） | 传递"这次构建产出的 image 和对应 commit"，保证 staging/prod 部署的就是 CI 验过的那一版 |
| `rollback.evaluate()` | rollback.py | health_gate.py、healthd.py | 部署门禁和运行期监控用**同一套阈值**，不会出现"门禁说健康、上线后立刻判定要回滚" |
| `ci/versions.json` | rollback.py | rollback.py、CD | 维护哪个 commit 是健康版本（good/bad），回滚时挑最近一个 good |
| `cd/capacity.json` | capacity.py | deploy.sh（--apply-capacity） | 资源预算结论：开几个 worker、每实例几线程 |
| `logs/health_history.json` | rollback.py | healthd | 连续异常计数，防抖动（默认连续 2 次才回滚） |

## 资源预算与并发开关（capacity.py）

实测基准（本机 ps 采样）：worker 单线程 18.9MB / 4 线程 19.7MB，API 进程 25MB，healthd 21MB，nginx 39MB。
机器：4 核 / 3723MB（可用 2835MB）。

算分两步，取小：

- **需求侧**：1 万用户在 60 分钟内提交完 → 到达率 2.78 任务/秒；DB 实测单任务 500ms、平均重试 1 次（放大 2 倍）→ 5.56 任务/秒；按服务台利用率 ≤70% + 安全系数 2 → **需要 8 并发**
- **资源侧**：内存可撑 292 并发、CPU 可撑 112 并发、SQLite 写串行经验上限 32 并发 → **上限 32 并发**

结论：**8 并发 = 2 个 worker 实例 × 4 线程**，预计内存 141MB（预算 1670MB）、CPU 5%。
因此当前把 worker 从 3 实例收敛到 2 实例（第三个超出预算，已回收）。

开关规则：

- 需求 ≤1 并发 → 关掉多线程，单实例单线程
- 需要扩容时优先加线程（每实例 4 线程封顶），再加实例（最多 6 个）
- 扩容前提：queue_depth 持续大于并发数，且 CPU <70%、内存 <75%
- 缩容前提：queue_depth 长时间为 0 且 CPU <20%

参数都能用环境变量覆盖，例如改提交窗口：

```bash
USERS=10000 WINDOW_MIN=10 python3 cd/capacity.py     # 10 分钟内涌进来 → 需求并发变 5 倍
```

## 常用命令

```bash
bash ci/ci.sh                                  # 只跑 CI
bash cd/deploy.sh --env staging                # 只部署 staging
bash cd/deploy.sh --env prod                   # staging → prod（含健康门禁 + 失败回滚）
python3 cd/capacity.py                         # 看资源预算与并发计划（不生效）
python3 cd/capacity.py --apply                 # 按计划调整 systemd 的 worker 实例/线程
python3 cd/health_gate.py --url http://127.0.0.1:8001/api/health --times 3 --interval 5
python3 rollback.py --show                     # 看当前健康指标、任务分布、版本记录
python3 rollback.py --record good              # 手工把当前版本标为健康基线
python3 rollback.py --to <commit>              # 手工回滚
```

## 模拟的部分（本机没有，按"没有就模拟"处理）

- 远端代码仓库 → 本地裸仓库 `/home/ubuntu/screenshot-api-remote.git`
- 镜像仓库 / 漏洞库 → 本地 docker + `ci/scan.py` 里的模拟 CVE 条目
- CI Runner → 直接在本机跑 `ci.sh`

## 与真实生产的差异（知道就好）

- **prod 目前是 systemd 跑源码，不是容器**。因为 nginx 的 upstream 指向 systemd 的 8001-8003；
  真实做法是镜像推到镜像仓库后由 K8s / 云容器服务做滚动更新，health probe 直接换成 readinessProbe。
  CD 脚本里 prod 这一步用"切到同一 commit + 重启"等价实现，image 和源码来自同一个 commit。
- **没有真正的灰度/流量切换**，真实做法是分批切流量 + 每批之间做健康门禁。
- **没有 429 限流**，真实做法是在 nginx 或网关层加。
