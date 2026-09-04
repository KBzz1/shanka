"""限流维度客户端 IP 解析（structure-contract 1.6；R25-07 同批加固）。

生产链路（CF 边缘 → cloudflared 回环 → uvicorn 仅绑 127.0.0.1，run.sh 显式
--forwarded-allow-ips=127.0.0.1）下 request.client.host 是回环地址，真实客户端
IP 由 Cloudflare 注入 ``CF-Connecting-IP``。uvicorn 不监听公网，外部无法直连
伪造该头，故存在即采信；本地直连（联调/测试）无此头，回退 request.client.host
（XFF 场景由 uvicorn proxy headers 重写 client.host 后同样落入回退分支）。
"""

from fastapi import Request

_CF_CONNECTING_IP = "CF-Connecting-IP"


def resolve_client_ip(request: Request) -> str:
    """限流键用客户端 IP：CF-Connecting-IP 优先，缺省回退 transport 层 client.host。"""
    cf_ip = request.headers.get(_CF_CONNECTING_IP, "").strip()
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"
