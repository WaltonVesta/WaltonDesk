"""
IP地址获取
支持IPv4/IPv6，多DNS备选
"""

import socket


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
        except Exception:
            continue
    
    return None


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