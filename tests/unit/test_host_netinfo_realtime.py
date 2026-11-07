"""本机地址实时取数（rdc.host.netinfo，走查第十四批 Bug 1）单元测试。

主断言面：
1. 每次调用即查询（无进程级缓存——隐私临时地址轮换后旧值必然连不上）；
2. 离线回落多网卡枚举（127.0.0.1 → 局域网优选），地址标明协议；
3. 探测面异常降级不抛（托盘展示路径宽容）。
"""

from __future__ import annotations

import socket

import pytest

from rdc.host import netinfo


class TestGetPreferredIpv4:
    def test_default_route_hit_returned_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(netinfo, "get_local_ip_v4", lambda: "192.168.1.10")
        called: list[bool] = []

        def _boom(*a: object) -> None:
            called.append(True)
            raise AssertionError("默认路由命中时不应再枚举接口")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        assert netinfo.get_preferred_ipv4() == "192.168.1.10"

    def test_offline_falls_back_to_lan_interface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无外网出口（探测回落 127.0.0.1）→ 接口枚举挑局域网私网地址。"""
        monkeypatch.setattr(netinfo, "get_local_ip_v4", lambda: "127.0.0.1")
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.8.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.10", 0)),
        ]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
        monkeypatch.setattr(socket, "gethostname", lambda: "host")
        got = netinfo.get_preferred_ipv4()
        assert got in {"10.0.0.5", "192.168.1.10"}  # 私网候选，排除 169.254/127

    def test_probe_raises_falls_back_to_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(netinfo, "get_local_ip_v4", lambda: "127.0.0.1")

        def _fail(*a: object) -> None:
            raise OSError("no dns")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        monkeypatch.setattr(socket, "gethostname", lambda: "host")
        assert netinfo.get_preferred_ipv4() == "127.0.0.1"


class TestGetDisplayIps:
    def test_both_protocols_labelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(netinfo, "get_preferred_ipv4", lambda: "192.168.1.10")
        monkeypatch.setattr(netinfo, "get_local_ip_v6", lambda: "2409:8a55:abcd:1234::9")
        assert netinfo.get_display_ips() == [
            "IPv4: 192.168.1.10",
            "IPv6: 2409:8a55:abcd:1234::9",
        ]

    def test_v6_none_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(netinfo, "get_preferred_ipv4", lambda: "192.168.1.10")
        monkeypatch.setattr(netinfo, "get_local_ip_v6", lambda: None)
        assert netinfo.get_display_ips() == ["IPv4: 192.168.1.10"]

    def test_v6_link_local_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """link-local（fe80::）对端不可路由，展示出去只会害用户复制错地址。"""
        monkeypatch.setattr(netinfo, "get_preferred_ipv4", lambda: "192.168.1.10")
        monkeypatch.setattr(netinfo, "get_local_ip_v6", lambda: "fe80::1234")
        assert netinfo.get_display_ips() == ["IPv4: 192.168.1.10"]

    def test_no_module_level_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """核心防线：连续两次调用返回**当前**值——临时地址轮换后不得复读旧值。"""
        seq = ["2409:8a55::1", "2409:8a55::2"]
        idx = {"i": 0}

        def _rotate() -> str:
            v = seq[idx["i"]]
            idx["i"] = min(idx["i"] + 1, len(seq) - 1)
            return v

        monkeypatch.setattr(netinfo, "get_local_ip_v6", _rotate)
        monkeypatch.setattr(netinfo, "get_preferred_ipv4", lambda: "10.0.0.1")
        first = netinfo.get_display_ips()
        second = netinfo.get_display_ips()
        assert first[1] == "IPv6: 2409:8a55::1"
        assert second[1] == "IPv6: 2409:8a55::2"  # 第二次已换新地址
