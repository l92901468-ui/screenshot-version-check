# 生产环境容器化接管（已写好，未启用）

> 状态：**未启用**。prod 现在仍然由 systemd 跑源码，nginx upstream 指向 127.0.0.1:8001-8003。
> 本目录的脚本只是准备好，什么时候要切，手工执行一次 enable.sh 即可。

## 为什么要「接管」而不是直接上

现网已经跑着一套 systemd 服务（api@8001/8002/8003、worker@1/2、healthd），
CI/CD 的部署、健康检查、自动回滚全部基于 systemd + git 实现。
容器化的好处是环境一致、扩缩容快、回滚到某个镜像 tag 更干净，
但代价是**回滚链路要重建**（容器里没有 git 和 systemctl）。

所以这里的策略是：**渐进接管，随时可退**。

- 容器监听 9111/9112/9113，跟现网 8001-8003 完全不冲突，切换期间两套并存；
- 数据用 bind mount 挂宿主的 `data/`，容器和 systemd 读写**同一个 SQLite 文件**；
- 切换只是改 nginx upstream 的一行，回退就是把它改回来；
- 容器起不来或者健康检查不过，脚本自动 `restore_systemd()`，服务不中断。

## 关键设计决定

| 决定 | 原因 |
|---|---|
| 用 bind mount `/home/ubuntu/screenshot-api/data:/data`，不用 named volume | 两套栈必须共享同一份数据。用卷会各写一份，数据分叉，回滚时旧栈读到的是旧数据 |
| 容器以 `user: "1000:1001"`（宿主 ubuntu）运行 | 共享目录宿主侧属于 ubuntu，用镜像里的 appuser 会 `unable to open database file` |
| **不跑 healthd 容器** | 健康检查与自动回滚依赖 git + systemctl，容器里没有。继续用宿主 systemd 的 healthd |
| systemd 加 drop-in `data.conf` 指向 `data/` | 让 systemd 那套也从共享目录读，切换前后数据路径一致，回退时不用再搬一次 |
| 端口 911x | 避开现网，切换期间可以两边同时对比验证 |

## 启用（手工，需要人确认）

```bash
cd /home/ubuntu/screenshot-api
sudo bash cd/prod-container/enable.sh
```

脚本做 6 件事，任何一步失败都会调用 `restore_systemd()` 复原：

1. 备份数据库到 `data/backup-YYYYmmdd-HHMMSS.db`
2. 停 systemd 的 api/worker（healthd 保留，它还要继续做健康检查）
3. 把 `app.db` / `app.db-wal` / `app.db-shm` / `uploads` 搬到共享的 `data/`，
   并给 `screenshot-api@` / `screenshot-worker@` / `screenshot-healthd`
   三个 unit 写 drop-in `data.conf`，把 `DB_PATH`、`UPLOAD_DIR` 指向 `data/`；`daemon-reload`
4. `docker compose -f docker-compose.prod.yml up -d`（3 个 api + 2 个 worker）
5. 对 9111/9112/9113 做健康检查门禁；通过后改 nginx upstream
   8001-8003 → 9111-9113（先 `nginx -t`，改前备份成 `.bak.<时间戳>`，失败自动还原）
6. 打一次 `https://127.0.0.1:8443/api/health` 确认全链路通；最后 disable systemd 的 api/worker
   （注意是 disable 不是 stop，避免下次开机又起来抢端口）

## 回退

```bash
cd /home/ubuntu/screenshot-api
sudo bash cd/prod-container/disable.sh
```

反向操作：重新 enable systemd 三个服务 → nginx upstream 从最新的 `.bak.*` 还原
（没有备份就把 911x 改回 800x）→ `docker compose down`。
数据不用搬，`data/` 是两边共享的。

## 未启用的原因 / 启用前还要想清楚的

1. **回滚语义变了**：现在回滚是 `git reset --hard <commit>` + restart，
   容器化之后应该是「把 `IMAGE_TAG` 指回上一个 tag 然后 `up -d`」。
   `rollback.py` 现在只会 git 回滚，接管后要改。
2. **ci/versions.json 要记镜像 digest**，不然不知道上一个「好」的镜像是哪个。
3. **日志**：systemd 的 journal 和容器的 `docker logs` 是两套，
   现在 healthd 读的是文件日志，接管后要确认路径还能对上。
4. **端口**：容器监听 911x 是靠 docker 的端口映射，宿主上多一层转发，
   p95/p99 会略有变化，切换后要重新采集一次基线。
