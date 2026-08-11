# Cloudflare Tunnel 落地设计（2026-08-11）

## 1. 背景与目标

- 闪卡 App v2.1 后端（FastAPI + SQLite + 本地 PDF 存储）需要放到公网供 Android 前端访问。
- 现状：无公网 IP、无服务器；域名已在 Cloudflare 管理；后端跑在 WSL2（Ubuntu 24.04）。
- 场景：**内测/少量真实用户**；WSL2 机器**基本常开**；访问者大概率在大陆（延迟是实测项）。
- 用户后续可能把「后端 + 网络层」整体打包进 Docker，迁移到另一台电脑运行——本设计须为此铺路。

## 2. 方案决策

| 方案 | 结论 |
| --- | --- |
| A. Cloudflare Tunnel + conda 直跑 | ✅ **采用**（契约 deployment.md 已定稿） |
| B. Tunnel + 全容器化（Docker Desktop + compose） | 现在不引入（过度工程） |
| C. 香港 VPS + Docker | 现在不引入（内测规模提前；契约阶梯第 2 级备选） |
| ngrok / frp / Tailscale / PaaS / 国内备案 | 均不适用（免费版限制、需公网中转、境外卡、周期重），不做备选 |

**Docker 的明确回答**：现在不需要。cloudflared 在 WSL2 直接装二进制/apt；后端继续 conda 直跑（与仓库工具链一致）。Docker 只在该方案迁移时引入。

## 3. 立即落地（实施内容）

### 3.1 架构

```text
Android 前端 ──HTTPS──▶ api.<域名>（Cloudflare 边缘，自动 TLS）
                             │  Tunnel 出站长连接（无公网 IP / 开放端口）
                             ▼
                  cloudflared（WSL2 常驻，systemd）
                             ▼
                  localhost:8000 → FastAPI（conda run，CWD=main/）
```

### 3.2 统一配置与凭据：根 `.env`

路径 `/home/kbzz1/shanka_backend/.env`（权限 600、git 忽略，均已满足）。所有部署凭据只此一处：

| 键 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 已有 |
| `API_KEY_ENCRYPTION_KEY` | PUT /api-key 加密密钥（database-design 2.2，需要时启用） |
| `DATABASE_URL` | `sqlite:///./data/shanka.db`（相对 uvicorn CWD=`main/`） |
| `STORAGE_PATH` | `./data/storage`（同上） |
| `TUNNEL_TOKEN` | 本次新增：Cloudflare 隧道 `shanka` 令牌 |

运行前 `set -a; source ../.env; set +a` 注入进程环境变量（既有工作流），`app/config.py` 零改动。

### 3.3 数据目录集中

`DATABASE_URL` / `STORAGE_PATH` 指向 `main/data/` 单目录——SQLite + PDF 从此集中，将来 Docker 化一个卷挂载。

### 3.4 启动脚本 `scripts/run.sh`（仓库根）

职责：
1. 端口占用检测（契约 4.1）：8000 被占用则换 8001 并提示同步 Tunnel 路由；端口为启动参数（默认 8000）。
2. `cd main`；`set -a; source ../.env; set +a`。
3. `conda run -n shanka-backend uvicorn app.main:app --host 127.0.0.1 --port $PORT`。

### 3.5 Cloudflare 侧

1. Zero Trust → Networks → Tunnels → 创建命名隧道 **`shanka`**。
2. 记录 Tunnel Token → 写入根 `.env` 的 `TUNNEL_TOKEN`（不入仓库）。
3. 公共主机名：`api.<域名>` → `http://localhost:8000`（默认端口，随 3.4 可配置）。

### 3.6 cloudflared 安装（WSL2 = Ubuntu 24.04）

- Cloudflare 向导没有「WSL」选项（WSL 不是系统，真实发行版是 Ubuntu）——选 **Debian**（官方对 Ubuntu 的安装指令即用 Debian apt 源，deb 包 Debian/Ubuntu 通用）。
- 兜底（apt 源不顺时）：直接下载官方 Linux 二进制 `cloudflared-linux-amd64` 放 `/usr/local/bin/cloudflared`，任何发行版通用。
- 常驻：`wsl.conf` 加 `[boot] systemd=true`；systemd 服务跑 `cloudflared tunnel run --token $TUNNEL_TOKEN`（EnvironmentFile/包装脚本从根 .env 读）。

### 3.7 验收与加固

1. `/healthz`、`/readyz` 探活（契约 8.2）。
2. 真机移动网络访问 `api.<域名>`，实测延迟并记录到 deployment.md 第 7 节（大陆延迟阶梯决策输入）。
3. 加固（可后补）：WAF 规则给 `api.<域名>` 限流；`/metrics` 只走 `dev.api.<域名>` 或加 CF Access。

## 4. Docker 演进（未来，实施边界外）

### 4.1 目标形态（docker compose 双服务）

```yaml
services:
  backend:      # Dockerfile: python:3.12-slim + 生产锁文件
    volumes:
      - data:/app/data            # SQLite + PDF 都在 data 卷
    environment:
      DATABASE_URL: sqlite:////app/data/shanka.db
      STORAGE_PATH: /app/data/storage
      DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY}
      API_KEY_ENCRYPTION_KEY: ${API_KEY_ENCRYPTION_KEY}
  cloudflared:  # 官方镜像 cloudflare/cloudflared
    command: tunnel --no-autoupdate run --token ${TUNNEL_TOKEN}
```

### 4.2 代码零改动依据

`app/config.py` 的 `database_url` / `storage_path` 已是环境变量可覆盖——Docker 化只是换一组值。本次铺路（`main/data/` 集中 + 根 `.env` 统一）就是未来迁移的完整交付物。

### 4.3 迁移步骤（未来执行）

1. 打包：`main/data/`（数据库 + PDF）+ 根 `.env`（全部凭据）。
2. 新机器：`docker compose up`。
3. 隧道无需重建：同一 `TUNNEL_TOKEN` 在新机器运行即接管出站连接；DNS/主机名配置在 Cloudflare 侧不变。

### 4.4 YAGNI 边界

Dockerfile / compose / 生产锁文件现在不写，等明确迁移时实施（契约：迁移不碰代码层）。

## 5. 契约同步

- 本设计落地后需同步 `docs/Architecture/deployment.md`：隧道命名 `shanka`（替代示例 `shanka-api`）、根 .env 统一凭据存放、Debian 源安装说明、systemd 开启方式、新增 Docker 演进章节。
- 兼容性变更，不涉及 PRD。
