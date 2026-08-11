# Cloudflare Tunnel 落地实施计划（2026-08-11）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将闪卡后端（FastAPI）通过 Cloudflare Tunnel（隧道 `shanka`，公共主机名 `shanka.kbzz1.top`）暴露到公网，供 Android 前端访问；数据目录集中到 `main/data/`，为将来 Docker 化铺路。

**Architecture:** Android → `https://shanka.kbzz1.top`（CF 边缘 TLS）→ Tunnel 出站长连接 → cloudflared（WSL2 systemd 常驻）→ `localhost:8000` → FastAPI（conda run）。凭据统一存仓库根 `.env`（权限 600、git 忽略）；启动封装为 `scripts/run.sh`（端口检测 + source .env + conda uvicorn）。

**Tech Stack:** cloudflared 2026.1.2（已装 `/usr/local/bin/cloudflared`）、WSL2 Ubuntu 24.04 + systemd（已开启）、uvicorn + FastAPI（conda 环境 `shanka-backend`）、SQLite + 本地 PDF 存储。

## Global Constraints

- 根 `.env`：权限 600、git 忽略（`.gitignore` 已含 `/.env`）、不入仓库；运行前 `set -a; source ../.env; set +a` 注入进程环境变量。
- conda 环境 `shanka-backend`（Python 3.12）跑 uvicorn；禁止把依赖装到 base 或系统 Python。
- 端口默认 8000（契约 4.1 可配置）；`DATABASE_URL` / `STORAGE_PATH` 相对 uvicorn CWD（`main/`）。
- 探活端点 `/healthz`、`/readyz`（`app/api/probes.py`，豁免 X-Device-ID 鉴权）。
- 密钥/令牌（DEEPSEEK_API_KEY、CLOUDFLARED_SERVICE_INSTALL_TOKEN）禁止写入日志、命令历史可避免时尽量避免、测试报告、git。
- 文档同步：`deployment.md` 子域名规划表与实际键名保持与本次实施一致。

---

### Task 1: 启动脚本 `scripts/run.sh`

**Files:**
- Create: `scripts/run.sh`（仓库根）
- Modify: `scripts/`（新建目录）

**Interfaces:**
- Produces: `scripts/run.sh` —— 可执行脚本；`source ../.env` 读取 `DATABASE_URL` / `STORAGE_PATH`（Task 2 写入）；以 `conda run -n shanka-backend uvicorn ... --port $PORT` 启动，`PORT` 默认 8000、被占用自动换 8001。
- Consumes: 根 `.env`（Task 2 才写入完整键；脚本对缺失键不报错，此时用 config.py 默认值）。

- [ ] **Step 1: 创建 `scripts/run.sh`**

```bash
mkdir -p /home/kbzz1/shanka_backend/scripts
```

写入以下内容（Edit/Write 工具）：

```bash
#!/usr/bin/env bash
# 启动脚本（契约 4.1：端口检测 → 被占用换 8001 并提示同步 Tunnel 回源路由）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8000}"

port_in_use() {
  ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":${1}$"
}

if port_in_use "${PORT}"; then
  echo "[run.sh] 端口 ${PORT} 被占用，切换到 8001" >&2
  echo "[run.sh] 提示：需在 Cloudflare Dashboard 将公共主机名回源端口同步改为 8001" >&2
  PORT=8001
fi

if port_in_use "${PORT}"; then
  echo "[run.sh] 错误：端口 8000 与 8001 均被占用，无法启动" >&2
  exit 1
fi

cd "${REPO_ROOT}/main"
set -a
# shellcheck disable=SC1091
source ../.env
set +a
exec conda run -n shanka-backend uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
```

- [ ] **Step 2: 赋可执行权限并做语法检查**

Run: `chmod +x /home/kbzz1/shanka_backend/scripts/run.sh && bash -n /home/kbzz1/shanka_backend/scripts/run.sh`
Expected: 无输出（语法 OK），退出码 0。

- [ ] **Step 3: 空运行验证（只验证到端口检测与 .env 加载，不真正启动）**

说明：脚本 exec uvicorn，无法"空跑"；本步验证前置条件——`.env` 可被 source 且 conda 环境存在。

Run:
```bash
cd /home/kbzz1/shanka_backend && set -a && source .env && set +a \
  && conda run -n shanka-backend python -c "print('env ok')"
```
Expected: 输出 `env ok`。

- [ ] **Step 4: Commit**

```bash
cd /home/kbzz1/shanka_backend
git add scripts/run.sh
git commit -m "feat(scripts): run.sh 启动封装（端口检测 + 根 .env + conda uvicorn）"
```

---

### Task 2: 根 `.env` 增加数据目录配置

**Files:**
- Modify: `.env`（仓库根，权限 600、git 忽略；当前含 `DEEPSEEK_API_KEY` 与 `CLOUDFLARED_SERVICE_INSTALL_TOKEN`）

**Interfaces:**
- Consumes: 无（本任务只写配置）。
- Produces: 键 `DATABASE_URL=sqlite:///./data/shanka.db`、`STORAGE_PATH=./data/storage`（相对 uvicorn CWD=`main/`）——Task 3 迁移与 Task 5 验收依赖。

- [ ] **Step 1: 检查键是否已存在（幂等）**

Run: `grep -c '^DATABASE_URL=' /home/kbzz1/shanka_backend/.env; grep -c '^STORAGE_PATH=' /home/kbzz1/shanka_backend/.env`
Expected: 两个 0（若已有，跳到 Step 4，本计划假定无）。

- [ ] **Step 2: 追加两个键**

Run:
```bash
printf 'DATABASE_URL=sqlite:///./data/shanka.db\nSTORAGE_PATH=./data/storage\n' >> /home/kbzz1/shanka_backend/.env
```

- [ ] **Step 3: 验证加载（不打印任何密钥值）**

Run:
```bash
cd /home/kbzz1/shanka_backend/main && set -a && source ../.env && set +a \
  && conda run -n shanka-backend python -c \
  "from app.config import Settings; s=Settings(); print(s.database_url); print(s.storage_path)"
```
Expected: 输出 `sqlite:///./data/shanka.db` 与 `./data/storage`（环境变量已覆盖默认值）。

- [ ] **Step 4: 权限与忽略确认（防回归）**

Run: `ls -l /home/kbzz1/shanka_backend/.env | awk '{print $1}'; grep -q '^/.env$' /home/kbzz1/shanka_backend/.gitignore && echo ignored`
Expected: `-rw-------` 且输出 `ignored`。

- [ ] **Step 5: Commit（记录配置决策）**

```bash
cd /home/kbzz1/shanka_backend
git status --short .env   # 确认 .env 未进暂存（应无 .env 行）
git commit --allow-empty -m "docs: 根 .env 数据目录配置（git 忽略，不入仓库）" 
```
说明：`.env` 被 git 忽略、不提交；`--allow-empty` 仅用于记录本决策。

---

### Task 3: 迁移现有数据到 `main/data/`

**Files:**
- Move: `main/shanka.db` → `main/data/shanka.db`（192KB 现有数据库）
- Move: `main/storage/`（当前为空）→ `main/data/storage/`

**Interfaces:**
- Consumes: Task 2 的 `DATABASE_URL` / `STORAGE_PATH`。
- Produces: `main/data/` 单目录承载全部运行时数据（Docker 化铺路）；Task 5 验收依赖。

- [ ] **Step 1: 确认后端未在运行（迁移要求）**

Run: `ss -tln | grep -E ':8000|:8001' || echo "无 uvicorn 监听"`
Expected: `无 uvicorn 监听`（若有：先停掉，再继续）。

- [ ] **Step 2: 创建 data/ 并迁移**

Run:
```bash
cd /home/kbzz1/shanka_backend/main
mkdir -p data/storage
mv shanka.db data/shanka.db
mv storage/* data/storage/ 2>/dev/null || true   # storage 当前为空目录
rmdir storage
```

- [ ] **Step 3: 验证文件落位**

Run: `ls -l /home/kbzz1/shanka_backend/main/data/ /home/kbzz1/shanka_backend/main/data/storage/`
Expected: `data/shanka.db`（192512 字节左右）；`data/storage/` 存在；`main/storage` 与 `main/shanka.db` 已不存在。

- [ ] **Step 4: 用 run.sh 启动并探活**

Run（后台）: `/home/kbzz1/shanka_backend/scripts/run.sh`
Expected: uvicorn 启动日志，监听 8000。

Run: `curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/healthz && echo; curl -s http://localhost:8000/readyz`
Expected: `200` + `{"status":"ok"}` 类 JSON（readyz 校验 DB + 存储可写，即验证 data/ 布局有效）。

- [ ] **Step 5: 验证旧库未残留（新库为新文件）**

Run: `ls /home/kbzz1/shanka_backend/main/shanka.db 2>&1; sqlite3 /home/kbzz1/shanka_backend/main/data/shanka.db 'select count(*) from decks;' 2>/dev/null || conda run -n shanka-backend python -c "import sqlite3; print(sqlite3.connect('/home/kbzz1/shanka_backend/main/data/shanka.db').execute('select count(*) from decks').fetchone())"`
Expected: `No such file`（旧路径）+ 表存在（count 返回数值；`decks` 表来自 database-design）。

- [ ] **Step 6: Commit**

```bash
cd /home/kbzz1/shanka_backend
git add -A
git commit -m "chore: 数据目录集中到 main/data/（Docker 化铺路）"
```
说明：data/ 若有运行时文件需要进 `.gitignore` 时，在执行时补充 `main/data/` 忽略规则并同步 `.gitignore` 提交。

---

### Task 4: cloudflared 常驻服务

**Files:**
- Modify（系统级）: `/etc/systemd/system/cloudflared.service`（由 `cloudflared service install` 生成）
- 读: 根 `.env` 的 `CLOUDFLARED_SERVICE_INSTALL_TOKEN`

**Interfaces:**
- Consumes: 根 `.env` 的 `CLOUDFLARED_SERVICE_INSTALL_TOKEN`（用户已放入）。
- Produces: systemd 服务 `cloudflared`（active running）——Task 5 公网验收依赖。

- [ ] **Step 1: 确认 cloudflared 已装且版本可用**

Run: `cloudflared --version`
Expected: `cloudflared version 2026.1.2`（已装，无需重装；若未装则按 deployment.md 第 3 步 Debian apt 源安装）。

- [ ] **Step 2: 用安装令牌装为系统服务**

Run:
```bash
cd /home/kbzz1/shanka_backend
set -a && source .env && set +a
sudo cloudflared service install "$CLOUDFLARED_SERVICE_INSTALL_TOKEN"
```
说明：token 以命令参数短暂出现在进程列表（单用户机器可接受）；`.env` 权限 600，仅本用户与 root 可读。
Expected: 输出 `Successfully installed cloudflared as a system service` 类信息。

- [ ] **Step 3: 验证服务运行与隧道连接**

Run: `systemctl status cloudflared --no-pager | head -8`
Expected: `active (running)`。

Run: `sudo journalctl -u cloudflared --no-pager -n 20`
Expected: 出现隧道注册成功日志（`Registered tunnel connection` 类）；无认证错误。

- [ ] **Step 4: 确认隧道与主机名（Dashboard 已配）**

Run: `sudo cloudflared tunnel list`
Expected: 列出隧道 `shanka`（此命令需要服务凭证，位于 `/etc/cloudflared/`；若提示认证，检查服务安装是否成功——上一步日志为准）。
说明：公共主机名 `shanka.kbzz1.top → http://localhost:8000` 用户已在 Dashboard 配置（类型 HTTP），本步只确认隧道本身在线。

- [ ] **Step 5: Commit（无代码变更时的记录提交）**

```bash
cd /home/kbzz1/shanka_backend
git commit --allow-empty -m "ops: cloudflared 常驻服务安装（隧道 shanka）"
```

---

### Task 5: 端到端验收（公网探活 + 真机延迟实测）

**Files:**
- 读: `docs/Architecture/deployment.md` 第 7 节（实测记录处）
- 读: `main/app/api/probes.py`（探活端点）

**Interfaces:**
- Consumes: Task 1（run.sh 后端运行）、Task 3（data/ 就绪）、Task 4（cloudflared 在线）。
- Produces: 延迟实测记录（deployment.md 第 7 节）——大陆延迟阶梯决策的输入。

- [ ] **Step 1: 确认后端本地在线**

Run: `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/healthz`
Expected: `200`。

- [ ] **Step 2: 公网探活（经 CF 边缘）**

Run: `curl -s -o /dev/null -w 'healthz=%{http_code} 总耗时=%{time_total}s\n' https://shanka.kbzz1.top/healthz`
Expected: `healthz=200`（首次访问含 TLS 握手，耗时 1-3s 属正常）。

Run: `curl -s https://shanka.kbzz1.top/readyz`
Expected: 200 + 就绪 JSON（DB + 存储可写 → 证明 data/ 迁移后公网链路全通）。

- [ ] **Step 3: 验证边缘 TLS 证书**

Run: `echo | openssl s_client -connect shanka.kbzz1.top:443 -servername shanka.kbzz1.top 2>/dev/null | grep -m1 'CN\|subject='`
Expected: 显示 Cloudflare 签发的证书（CN 含 shanka.kbzz1.top 或 *.kbzz1.top）。

- [ ] **Step 4: 真机实测并记录延迟**

操作（需用户手机）：Android 手机切**移动网络**（关闭 Wi-Fi），浏览器访问 `https://shanka.kbzz1.top/healthz`，用开发者工具/网络面板或 `curl -w` 记录总耗时。

- [ ] **Step 5: 把实测结果写入 deployment.md 第 7 节**

追加到 `docs/Architecture/deployment.md` 第 7 节（保留原内容）：

```markdown
- 实测记录（2026-08-11）：
  - 本机 WSL2 curl：`<Step 2 的 time_total>`
  - 真机移动网络：`<Step 4 用户实测耗时>`
  - 结论：`<在阶梯 1 可用 / 延迟不可接受需评估阶梯 2>`（按实测填）
```

- [ ] **Step 6: Commit**

```bash
cd /home/kbzz1/shanka_backend
git add docs/Architecture/deployment.md
git commit -m "docs(deployment): 真机延迟实测记录（阶梯决策输入）"
```

---

### Task 6: 文档同步（deployment.md 子域名规划 + 键名统一）

**Files:**
- Modify: `docs/Architecture/deployment.md`（子域名规划表、键名引用）
- Modify: `docs/superpowers/specs/2026-08-11-cloudflare-tunnel-落地-design.md`（3.2/3.5 实际值同步）

**Interfaces:**
- Consumes: 全部前序任务的实际决策。
- Produces: 契约与实际部署一致（防漂移红线）。

- [ ] **Step 1: 更新 deployment.md 第 3 节子域名规划表**

将表格改为实际值：

```markdown
| 子域名 | 用途 | Tunnel 路由 |
| shanka.kbzz1.top | 生产 API(App 连接) | localhost:8000(默认,可配置) |
| dev.api.<domain> | 开发联调(可选,未配置) | 同上或独立端口 |
```

- [ ] **Step 2: 统一键名引用（deployment.md 全文）**

把 `TUNNEL_TOKEN` 全部替换为 `CLOUDFLARED_SERVICE_INSTALL_TOKEN`（第 2/4/7/8 节引用处）。

- [ ] **Step 3: 同步 spec 实际值**

`docs/superpowers/specs/2026-08-11-cloudflare-tunnel-落地-design.md`：
- 3.2 表：`TUNNEL_TOKEN` → `CLOUDFLARED_SERVICE_INSTALL_TOKEN`，说明改为「隧道 shanka 服务安装令牌」。
- 3.5：公共主机名 `shanka.kbzz1.top`（替代 `api.<域名>`）。
- 3.6：systemd 常驻方式改为 `cloudflared service install`（已确认 WSL2 systemd 开启、cloudflared 2026.1.2 已装）。

- [ ] **Step 4: 验证文档无残留旧值**

Run: `grep -rn 'TUNNEL_TOKEN\|api.<domain>' /home/kbzz1/shanka_backend/docs/Architecture/deployment.md /home/kbzz1/shanka_backend/docs/superpowers/specs/2026-08-11-cloudflare-tunnel-落地-design.md`
Expected: 无输出（无残留）。

- [ ] **Step 5: Commit**

```bash
cd /home/kbzz1/shanka_backend
git add docs/Architecture/deployment.md docs/superpowers/specs/2026-08-11-cloudflare-tunnel-落地-design.md
git commit -m "docs: 子域名规划与凭据键名同步实际值（shanka.kbzz1.top / CLOUDFLARED_SERVICE_INSTALL_TOKEN）"
```

---

## 验收清单（全部完成后）

- [ ] `scripts/run.sh` 存在、可执行，端口占用自动切换 8001。
- [ ] 根 `.env` 含 `DATABASE_URL` / `STORAGE_PATH` / `CLOUDFLARED_SERVICE_INSTALL_TOKEN`；权限 600、git 忽略。
- [ ] `main/data/` 含 `shanka.db` 与 `storage/`；旧路径无残留。
- [ ] `systemctl status cloudflared` = active；`cloudflared tunnel list` 见隧道 `shanka`。
- [ ] `curl https://shanka.kbzz1.top/healthz` = 200；`/readyz` = 200。
- [ ] deployment.md 第 7 节含 2026-08-11 实测记录；子域名表与键名已同步。
- [ ] 全部提交入库（`.env` 未入 git）。
