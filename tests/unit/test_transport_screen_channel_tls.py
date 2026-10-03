"""screen_channel._upgrade_tls 分支补测（批 5 覆盖率收口）。

既有集成/服务级测试全走"自签名一次成功"主路径，本文件覆盖三条降级防线与
重复调用守卫：

- 非自签名错误（过期/主机名不符…）**拒绝降级**（v4.0.2 病灶的契约面）；
- 自签名降级后重握手失败 → TlsError；
- 降级后 TOFU 用户拒绝 → 关闭并重抛；
- connect 重复调用 → RuntimeError。

替身 writer 按脚本顺序抛错/成功，不触网络。
"""

from __future__ import annotations

import asyncio
import ssl

import pytest

from rdc.exceptions import TlsError
from rdc.transport import screen_channel as sc_mod
from rdc.transport.base import FrameStream
from rdc.transport.screen_channel import ScreenChannel
from tests._transport_support import FakeSSLObject

pytestmark = pytest.mark.unit


class _ScriptedWriter:
    """start_tls 按脚本依次抛错/成功的写端替身。"""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.closed = False
        self.ssl_object: object = None

    async def start_tls(self, _ctx: object, *, server_hostname: str | None = None) -> None:
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "ssl_object":
            return self.ssl_object
        if name == "peername":
            return ("127.0.0.1", 4433)
        return default


def _channel(monkeypatch: pytest.MonkeyPatch, writers: list[_ScriptedWriter]) -> ScreenChannel:
    """构造通道并把 open_connection 换成依次发放替身 writer 的假实现。"""

    async def fake_open(_host: str, _port: int):  # noqa: ANN202
        w = writers.pop(0)
        return asyncio.StreamReader(), w

    monkeypatch.setattr(sc_mod.asyncio, "open_connection", fake_open)
    return ScreenChannel(
        "127.0.0.1",
        9999,
        session_token="tok",
        connection_id=b"c" * 32,
        fingerprint_confirm=lambda _fp, _h: True,
    )


class TestTlsFallbackGuards:
    async def test_non_self_signed_error_refuses_downgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """过期证书 → 直接 TlsError，绝不走 CERT_NONE 降级、不再开第二条连接。"""
        w1 = _ScriptedWriter([ssl.SSLError("certificate has expired")])
        writers = [w1]
        ch = _channel(monkeypatch, writers)
        with pytest.raises(TlsError, match="拒绝降级"):
            await ch.connect()
        assert w1.closed  # 首连接已关闭
        assert writers == []  # 未重建第二条连接（降级被拒）

    async def test_fallback_rehandshake_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自签名允许降级，但降级重握手再失败 → TlsError（降级重握手失败）。"""
        w1 = _ScriptedWriter([ssl.SSLError("self signed certificate")])
        w2 = _ScriptedWriter([ssl.SSLError("bad record mac")])
        ch = _channel(monkeypatch, [w1, w2])
        with pytest.raises(TlsError, match="降级重握手失败"):
            await ch.connect()
        assert w1.closed and w2.closed

    async def test_fallback_tofu_decline_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """降级成功但用户拒绝指纹 → 关闭重连并上抛（TOFU 拒绝路径）。"""
        w1 = _ScriptedWriter([ssl.SSLError("self-signed certificate")])
        w2 = _ScriptedWriter([None])
        w2.ssl_object = FakeSSLObject(b"der-bytes")
        ch = ScreenChannel(
            "127.0.0.1",
            9999,
            session_token="tok",
            connection_id=b"c" * 32,
            fingerprint_confirm=lambda _fp, _h: False,  # 用户点"拒绝"
        )

        async def fake_open(_host: str, _port: int):  # noqa: ANN202
            return asyncio.StreamReader(), writers.pop(0)

        writers = [w1, w2]
        monkeypatch.setattr(sc_mod.asyncio, "open_connection", fake_open)
        with pytest.raises(TlsError, match="用户拒绝"):
            await ch.connect()
        assert w2.closed

    async def test_double_connect_rejected(self) -> None:
        """已绑定流的通道再次 connect → RuntimeError（只能调用一次）。"""
        ch = ScreenChannel("127.0.0.1", 9999, session_token="t", connection_id=b"x" * 32)
        ch.bind_stream(FrameStream(asyncio.StreamReader(), _ScriptedWriter([])))
        with pytest.raises(RuntimeError, match="只能调用一次"):
            await ch.connect()
