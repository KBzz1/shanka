# 部署设计（Cloudflare Tunnel）v2.1

## 1. 现状与目标
域名已在 Cloudflare 管理;无公网 IP / 无服务器;目标:Android 前端经 HTTPS 访问后端 API。
**实施时机**:实际接入(P3-4,最后阶段);本文档为设计定稿。

## 2. 架构
(规格 6.1 架构图:Android → api.<domain>(CF 边缘) → Tunnel → cloudflared(WSL2) → FastAPI)

```text
Android 前端 ──HTTPS──▶ api.<domain>（Cloudflare 边缘，DNS + 自动 TLS 证书）
                             │  Tunnel（出站长连接，无公网 IP / 开放端口）
                             ▼
                    cloudflared（WSL2 本地常驻）
                             │
                             ▼  http://localhost:8000
                    FastAPI（main）
```

## 3. 子域名规划
| 子域名 | 用途 | Tunnel 路由 |
| api.<domain> | 生产 API(App 连接) | localhost:<port>(默认 8000,可配置) |
| dev.api.<domain> | 开发联调 | 同上或独立端口 |

## 4. 接入步骤(P3-4 执行)
1. 端口检测:启动前检查占用,被占用则换端口并同步 Tunnel 路由;FastAPI 监听端口为配置项(环境变量覆盖)。
2. Cloudflare Zero Trust → Networks → Tunnels → 创建命名隧道 `shanka`,记录 Tunnel Token。
3. WSL2 安装 cloudflared:向导无「WSL」选项(WSL 非系统,真实发行版为 Ubuntu),选 **Debian** apt 源即可(官方对 Ubuntu 的指令即用 Debian 源,deb 包 Debian/Ubuntu 通用);apt 源不顺时兜底:直接下载官方二进制 `cloudflared-linux-amd64` 放 `/usr/local/bin/cloudflared`。
4. WSL2 常驻:`wsl.conf` 加 `[boot] systemd=true`;systemd 服务跑 `cloudflared tunnel run --token $TUNNEL_TOKEN`,EnvironmentFile/包装脚本从根 `.env` 读取;后端由 `scripts/run.sh` 拉起(端口占用检测→换端口并同步 Tunnel 路由)。
5. 公共主机名配置:`api.<domain>` → `localhost:<port>`。
6. TLS:边缘自动 HTTPS;回源走 Tunnel 内部加密,不暴露端口。
7. 可选加固:WAF 自定义规则(限流);`/metrics` 只走 dev 子域名或加 Access。

## 5. 与契约衔接
- 契约 1.7(HTTPS):边缘层 TLS 终止,天然满足。
- 契约 1.6(应用层限流)为兜底;CF 边缘限流为外层防线,两层互补。
- /healthz、/readyz 供 Tunnel/监控探活。

## 6. 大陆访问延迟:阶梯决策
| 阶段 | 方案 | 成本 |
| MVP 开发联调 | Tunnel + CF 边缘,真机实测(移动网络通常走香港节点,100~250ms) | 零 |
| 实测不可接受 | 灰云 + 香港 VPS:CF 只做 DNS(灰云),Nginx + Let's Encrypt(CF DNS-01 challenge);自管反代/证书/防火墙 | 约 $5/月 |
| 真实大陆用户 | 国内云 + ICP 备案(服务器 + 域名双备案) | 最高 |

- 灰云与 Tunnel 不兼容;升级 = 部署 VPS → 改 DNS(橙云变灰云)→ 迁移证书;代码层不受影响。
- 决策依据:低频短请求 + 前端 Room 缓存,100~250ms 无感知差异;MVP 不为规模问题提前优化。

## 7. 运维注意
- cloudflared 常驻与自启;Tunnel Token 为敏感凭据,统一放仓库根 `.env`(`DEEPSEEK_API_KEY` 同款:权限 600、git 忽略,不入仓库),systemd/脚本从 `.env` source,禁止散落各处。
- 延迟/连通性实测记录处(迁移决策输入)。
- 实测记录(2026-08-11):
  - 本机 WSL2 curl:0.485/0.571/0.575s(首连 1.550s 含 TLS 握手;`--noproxy '*'` 直连 https://shanka.kbzz1.top/healthz,三次重复)。
  - 真机移动网络:306 ms(Android 移动网络,浏览器网络面板,healthz 200)。
  - 结论:阶梯 1 可用(Tunnel + CF 边缘,零成本)——306ms 略高于预期上限 250ms,但低频短请求 + 前端 Room 缓存下无感知差异,MVP 继续;若后续体验卡顿再评估阶梯 2(灰云 + 香港 VPS)。

## 8. Docker 演进(可选迁移形态,实施边界外)
当需要把「后端 + 网络层」整体迁移到另一台电脑时,docker compose 双服务:

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

- 代码零改动依据:`app/config.py` 的 `database_url`/`storage_path` 已是环境变量可覆盖;迁移交付物 = `main/data/` 目录 + 根 `.env`。
- 迁移步骤:打包 `data/` + `.env` → 新机器 `docker compose up`;隧道无需重建,同一 `TUNNEL_TOKEN` 运行即接管出站连接(DNS/主机名在 CF 侧不变)。
- YAGNI:Dockerfile/compose/生产锁文件在明确迁移时再实施,当前不引入。
