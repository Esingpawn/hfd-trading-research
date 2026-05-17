# HFD Linux 部署说明

本文档用于 Linux 服务器部署。Windows PowerShell 脚本只保留给本地开发，不作为生产运行方式。

## 1. 目标形态

服务器使用 Docker Compose 管理：

- `api`: FastAPI 服务。
- `collector-worker`: 分层采集循环。
- `paper-worker`: 纸上交易循环。
- `experiment-worker`: 信号结果回填与实验标签循环。
- `postgres`: PostgreSQL 主数据库。
- `redis`: 后续缓存、队列、锁。

## 2. 前置条件

服务器需要安装：

- Docker
- Docker Compose v2
- Git

建议目录：

```bash
/opt/hfd
```

## 3. 首次部署

```bash
git clone https://github.com/Esingpawn/hfd-trading-research.git /opt/hfd
cd /opt/hfd
cp .env.production.example .env
```

编辑 `.env`，至少修改：

```text
POSTGRES_PASSWORD=change_me
HFD_API_PORT=8000
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

If port 8000 is already used on the server, set `HFD_API_PORT` to another host port, for example `18000`.

启动：

```bash
bash scripts/linux/bootstrap.sh
```

面板地址：

```text
http://服务器IP:${HFD_API_PORT}/dashboard
```

## 4. 日常命令

查看状态：

```bash
bash scripts/linux/status.sh
```

查看日志：

```bash
bash scripts/linux/logs.sh api
bash scripts/linux/logs.sh collector-worker
bash scripts/linux/logs.sh paper-worker
bash scripts/linux/logs.sh experiment-worker
```

数据库维护：

```bash
bash scripts/linux/maintain-db.sh
```

数据库备份：

```bash
bash scripts/linux/backup-postgres.sh
```

## 5. 更新部署

当前生产环境使用 Git 工作区 `/opt/hfd-git.tmp` 部署。`docker-compose.yml` 已固定 `name: hfd`，因此从该目录运行 Compose 仍会使用现有 `hfd_*` 容器和数据卷。

本地推荐部署命令：

```powershell
git status --short
git push production main
.\scripts\deploy-production.ps1 -Services api,darkflow-worker
```

`production` remote 通过 SSH 推送到服务器工作区，不依赖服务器从 GitHub 拉取代码：

```text
ssh://root@124.221.31.75:2222/opt/hfd-git.tmp
```

GitHub 网络正常时可以额外加 `-PushOrigin`：

```powershell
.\scripts\deploy-production.ps1 -Services api,darkflow-worker -PushOrigin
```

服务器上的 `.env` 是本地运行配置，必须保留在服务器且不纳入 Git。不要在生产环境执行 `docker compose down -v`，这会删除数据库和运行时数据卷。

旧的服务器内拉取方式如下，只适用于服务器 GitHub 网络稳定时：

```bash
cd /opt/hfd
git pull
docker compose build
docker compose run --rm api alembic upgrade head
docker compose up -d
```

## 6. 当前边界

当前 Docker Compose 已完成服务器基础运行形态，但还不是最终生产架构。

后续仍需继续完成：

- Alembic migration。
- raw payload 外置压缩。
- Redis 任务队列。
- worker 任务状态表。
- Nginx/Caddy 反向代理与 HTTPS。
- 更完整的备份恢复流程。
- 实盘交易安全门禁。

## 7. 安全要求

- 不提交 `.env`。
- 不提交数据库文件。
- 不提交 raw payload 数据。
- 不把 `docker compose config` 的完整输出粘贴到公开位置，因为它会展开 `.env` 中的密钥。
- 实盘交易默认关闭。
- 生产服务器必须配置防火墙，只开放必要端口。
