"""
IP地址获取
支持IPv4/IPv6，多DNS备选

走查第十四批 Bug 1：本模块**每次调用即查询**（无任何启动期缓存语义）；
``get_display_ips`` 为托盘展示/复制的唯一实时取数入口——Windows IPv6 隐私
临时地址周期性轮换、Wi-Fi/网线切换改变路由，托盘任何"上次取到的 IP"都会
过时（用户复制旧地址必然连不上）。
"""

import socket

__all__ = [
    "get_all_ips",
    "get_display_ips",
    "get_ip_family",
    "get_local_ip_v4",
    "get_local_ip_v6",
    "get_preferred_ipv4",
    "is_ipv4",
    "is_ipv6",
]


def get_local_ip_v4() -> str:
    """获取本地IPv4地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _is_link_local_or_loopback(ip: str) -> bool:
    """127./169.254. 本机不可达形态（回环 / APIPA）。"""
    return ip.startswith("127.") or ip.startswith("169.254.")


def get_preferred_ipv4() -> str:
    """当前对外 IPv4：默认路由源地址优先；离线（探测回落 127.0.0.1）时
    从本机接口枚举挑一个非回环/非 APIPA 的局域网地址（走查第十四批：
    "尽量优先展示可用的局域网 IP"——纯内网/无外网出口机器按旧实现只会
    显示 127.0.0.1，用户照样复制不到能用的地址）。"""
    ip = get_local_ip_v4()
    if not _is_link_local_or_loopback(ip):
        return ip
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return ip
    cands = sorted(
        {str(i[4][0]) for i in infos} - {ip} - {"127.0.0.1"},
        key=_lan_rank,
    )
    for cand in cands:
        if not _is_link_local_or_loopback(cand):
            return cand
    return ip


def _lan_rank(ip: str) -> tuple[int, str]:
    """局域网私网优先排序：RFC1918 私网段 0，其余 1；同档按字面稳定排。"""
    import ipaddress

    try:
        v = ipaddress.ip_address(ip)
    except ValueError:
        return (2, ip)
    return (0 if v.is_private else 1, ip)


def get_local_ip_v6(dns_servers: list[str] | None = None) -> str | None:
    """
    获取公网IPv6地址
    
    Args:
        dns_servers: DNS服务器列表（备选）；为 None 时使用内置默认列表。
    """
    if dns_servers is None:
        dns_servers = [
            '2001:4860:4860::8888',  # Google DNS
            '2606:4700:4700::1111',  # Cloudflare DNS
            '2400:3200::1',          # 阿里DNS
            '240c::6666',            # 114 DNS
        ]
    
    for dns in dns_servers:
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s.connect((dns, 80))
            ip = s.getsockname()[0]
            s.close()
            # 过滤链路本地地址
            if not ip.startswith('fe80::'):
                return ip
        except Exception:  # 探测面异常不可枚举（平台差异），轮询语义下继续下一网关
            continue  # nosec B112 -- 多 DNS 网关逐一探测属设计行为；本模块为 v4 遗留面，异常收窄与重构挂阶段 10
    
    return None


def get_display_ips() -> list[str]:
    """托盘展示用：实时获取本机全部可用地址，按优先级排序并标明协议。

    走查第十四批 Bug 1（Windows IPv6 隐私临时地址周期性跳变）：调用方
    **不得缓存**本函数结果——托盘每次弹出/每次复制都必须重新调用，
    否则用户拿到的就是上一个临时地址，连不上。

    Returns:
        形如 ``["IPv4: 192.168.1.10", "IPv6: 2409:8a55:..."]`` 的列表；
        无可用 v6（纯 v4 网络/探测失败）时只返回 v4 项。排序稳定：IPv4 在前。
    """
    out = [f"IPv4: {get_preferred_ipv4()}"]
    v6 = get_local_ip_v6()
    if v6 and not v6.startswith("fe80::"):
        out.append(f"IPv6: {v6}")
    return out


def get_all_ips() -> dict:
    """获取所有IP地址"""
    return {
        'ipv4': get_local_ip_v4(),
        'ipv6': get_local_ip_v6()
    }


def is_ipv6(address: str) -> bool:
    """检查是否为IPv6地址"""
    return ':' in address


def is_ipv4(address: str) -> bool:
    """检查是否为IPv4地址"""
    return '.' in address and not is_ipv6(address)


def get_ip_family(address: str) -> int:
    """获取IP地址族"""
    if is_ipv6(address):
        return socket.AF_INET6
    return socket.AF_INET