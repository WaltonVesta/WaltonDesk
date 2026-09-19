"""
控制指令通道
独立Socket，小包传输，实时性优先
"""

import json
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable

from rdc.constants import HANDSHAKE_HEADER_LEN
from rdc.logger import get_logger
from rdc.protocol.framing import Packet, PacketBuffer


class ControlChannel:
    """控制指令通道"""
    
    def __init__(self):
        self.sock: socket.socket | None = None
        self.buffer = PacketBuffer()
        self._connected = False
        self._stop = False
        self._recv_thread: threading.Thread | None = None
        self._on_packet: Callable | None = None
        self.logger = get_logger()
        self._lock = threading.Lock()
        self._last_recv = time.time()
        self._tls_context: ssl.SSLContext | None = None
        self.session_token: str | None = None  # v4.0.1：会话令牌（用于画面通道认证）
        self.password_callback: Callable[[], str | None] | None = None  # v4.0.1：主线程密码弹窗回调
        self.fingerprint_callback: Callable[[str, str], bool] | None = None  # v4.0.1：证书指纹确认回调
    
    def connect(self, host: str, port: int) -> bool:
        """连接（v4.0.0：强制TLS + 长度前缀握手）"""
        try:
            is_ipv6 = ':' in host
            family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
            raw_sock = socket.socket(family, socket.SOCK_STREAM)
            # 开启TCP_NODELAY，禁用Nagle算法
            raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw_sock.settimeout(5.0)
            raw_sock.connect((host, port))

            # ===== 强制 TLS 包装（v4.0.0） =====
            # v4.0.2：传入 port，供降级重建 socket 时使用
            self.sock = self._wrap_tls(raw_sock, host, port)
            self.sock.settimeout(None)

            # ===== 认证流程（Handshake V4：长度前缀） =====
            from rdc.config._v4_config import Config
            config = Config()
            enable_auth = config.get('enable_auth', False)

            if enable_auth:
                password_hash = config.get('password_hash', '')
                if password_hash:
                    # 需要输入密码（v4.0.1：优先使用主线程回调，避免子线程创建Tk）
                    if self.password_callback:
                        password = self.password_callback()
                    else:
                        password = self._prompt_password()
                    if not password:
                        self.logger.error("未提供密码")
                        self.sock.close()
                        self.sock = None
                        return False
                else:
                    # v4.0.3 修复：启用认证时，即使无需密码也必须完成握手交换。
                    # 旧版在此跳过发送与读取，导致拿不到服务端发放的会话令牌，
                    # 画面通道令牌认证失败、连接被服务端关闭（连接成功但无画面）。
                    password = ''

                # 发送带长度前缀的认证数据
                auth_data = json.dumps({'password': password}).encode('utf-8')
                self.sock.sendall(struct.pack('>I', len(auth_data)) + auth_data)

                # 读取长度前缀响应（v4.0.1：JSON格式，包含会话令牌）
                self.sock.settimeout(5.0)
                response = self._recv_length_prefixed()
                self.sock.settimeout(None)

                auth_status = ""
                if response:
                    try:
                        resp_json = json.loads(response.decode('utf-8'))
                        auth_status = resp_json.get('status', '')
                        self.session_token = resp_json.get('token')
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        auth_status = response.decode('utf-8', errors='ignore')

                if auth_status != 'AUTH_OK':
                    self.logger.error(f"认证失败: {auth_status or response!r}")
                    self.sock.close()
                    self.sock = None
                    return False
                self.logger.info("认证成功")
            # ===== 认证结束 =====

            self._connected = True
            self._stop = False
            self._last_recv = time.time()
            self.logger.info(f"控制通道已连接: {host}:{port}")
            self._start_thread()
            return True
        except Exception as e:
            self.logger.error(f"控制通道连接失败: {e}")
            # v4.0.2：确保异常时彻底清理 socket，避免后续使用已关闭的句柄
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    # 清理失败不阻断：句柄随后仍会被置空丢弃，此处仅记录以便排查泄漏。
                    self.logger.debug("连接失败清理时关闭 socket 异常", exc_info=True)
                self.sock = None
            return False

    def _wrap_tls(self, raw_sock: socket.socket, host: str, port: int) -> ssl.SSLSocket:
        """包装TLS（v4.0.2：修复Windows下自签名证书降级导致的非套接字错误）

        1. 优先使用系统 CA 证书库 + 主机名校验（防 MITM）。
        2. 若握手失败且确认为自签名证书，降级为 CERT_NONE，
           获取证书 SHA-256 指纹，查信任列表。
        3. 首次连接的指纹通过 fingerprint_callback 弹窗让用户确认，
           用户确认后加入信任列表（TOFU），后续自动信任。
        4. 指纹不匹配或用户拒绝则关闭连接并抛出异常。

        v4.0.2 修复：
        - 降级重建 socket 时直接使用 host/port 参数，避免依赖可能已损坏的
          raw_sock.getpeername()（Windows 下触发 WinError 10038）。
        - 仅在异常信息确认为自签名证书时才降级，其他证书错误直接抛出。
        """
        from rdc.security.fingerprint import FingerprintManager, get_fingerprint_manager

        # ---- 第一步：尝试系统 CA 校验 ----
        try:
            ca_context = ssl.create_default_context()
            ca_context.minimum_version = ssl.TLSVersion.TLSv1_2
            wrapped = ca_context.wrap_socket(raw_sock, server_hostname=host)
            self._tls_context = ca_context
            self.logger.info("TLS连接已建立（系统CA校验通过）")
            return wrapped
        except ssl.SSLError as e:
            # v4.0.2：仅在确认为自签名证书时才降级，其他证书错误（过期、吊销、主机名不匹配等）直接抛出
            err_msg = str(e).lower()
            if 'self signed certificate' not in err_msg and 'certificate_verify_failed' not in err_msg:
                self.logger.warning(f"TLS握手失败（非自签名证书），不降级: {e}")
                raise
            self.logger.info(f"系统CA校验失败（自签名证书），切换自签名证书模式: {e}")
            # v4.0.2：关闭可能已损坏的旧 socket，使用已知 host/port 重建
            try:
                raw_sock.close()
            except OSError:
                # 旧 socket 可能已损坏，关闭失败不影响重建；记录以便排查句柄泄漏。
                self.logger.debug("TLS 降级时关闭旧 socket 异常", exc_info=True)
            is_ipv6 = ':' in host
            family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
            raw_sock2 = socket.socket(family, socket.SOCK_STREAM)
            raw_sock2.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw_sock2.settimeout(5.0)
            raw_sock2.connect((host, port))
            raw_sock = raw_sock2
        except Exception as e:
            self.logger.warning(f"TLS系统CA握手异常: {e}")
            raise

        # ---- 第二步：自签名证书模式（CERT_NONE + 指纹信任） ----
        self_signed_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self_signed_context.check_hostname = False
        self_signed_context.verify_mode = ssl.CERT_NONE
        self_signed_context.minimum_version = ssl.TLSVersion.TLSv1_2

        wrapped = self_signed_context.wrap_socket(raw_sock, server_hostname=host)
        self._tls_context = self_signed_context

        # 获取证书指纹
        der = wrapped.getpeercert(binary_form=True)
        if not der:
            self.logger.warning("无法获取对端证书，拒绝连接")
            wrapped.close()
            raise ssl.SSLError("无法获取对端证书")

        fingerprint = FingerprintManager.compute_fingerprint(der)
        self.logger.info(f"TLS自签名证书指纹(SHA256): {fingerprint}")

        # 检查信任列表
        fp_manager = get_fingerprint_manager()
        if fp_manager.is_trusted(fingerprint, host):
            self.logger.info("证书指纹已在信任列表中，自动信任")
            return wrapped

        # 首次连接：请求用户确认
        if self.fingerprint_callback:
            confirmed = self.fingerprint_callback(fingerprint, host)
        else:
            # 无回调时默认信任（兼容旧调用方），但记录警告
            self.logger.warning("未设置指纹确认回调，默认信任自签名证书（存在MITM风险）")
            confirmed = True

        if confirmed:
            fp_manager.trust(fingerprint, host)
            return wrapped
        else:
            self.logger.warning("用户拒绝证书指纹，关闭连接")
            wrapped.close()
            raise ssl.SSLError("用户拒绝证书指纹")

    def _recv_length_prefixed(self) -> bytes | None:
        """读取4字节大端长度前缀 + Payload（Handshake V4）"""
        try:
            length_data = self._recv_exactly(HANDSHAKE_HEADER_LEN)
            if length_data is None or len(length_data) < HANDSHAKE_HEADER_LEN:
                return None
            length = struct.unpack('>I', length_data)[0]
            if length > 4096:
                self.logger.warning(f"握手包长度超限: {length}")
                return None
            return self._recv_exactly(length)
        except Exception as e:
            self.logger.error(f"读取长度前缀握手失败: {e}")
            return None

    def _recv_exactly(self, n: int) -> bytes | None:
        """精确读取n字节"""
        data = b''
        while len(data) < n:
            # self.sock 可能被另一线程（close/_disconnect）置为 None，
            # 取局部引用并判空，避免跨线程竞态下的 AttributeError。
            sock = self.sock
            if sock is None:
                return None
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def get_tls_context(self) -> ssl.SSLContext | None:
        """获取TLS上下文（供画面通道复用）"""
        return self._tls_context

    def get_session_token(self) -> str | None:
        """v4.0.1：获取会话令牌（供画面通道认证）"""
        return self.session_token
    
    def _prompt_password(self) -> str:
        """弹出密码输入框"""
        try:
            import tkinter as tk
            from tkinter import simpledialog
            root = tk.Tk()
            root.withdraw()
            password = simpledialog.askstring("认证", "请输入被控端密码:", show='*')
            root.destroy()
            return password or ""
        except Exception:
            # 如果没有GUI，从命令行输入
            import getpass
            return getpass.getpass("请输入被控端密码: ")

    def _start_thread(self) -> None:
        """启动接收线程"""
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
    
    def _recv_loop(self) -> None:
        """接收循环"""
        while self._connected and not self._stop:
            try:
                sock = self.sock
                if sock is None:
                    # 套接字已被另一线程关闭，结束循环走统一断开清理。
                    break
                data = sock.recv(4096)
                if not data:
                    self.logger.warning("控制通道接收断开")
                    break
                self._last_recv = time.time()
                self.buffer.append(data)
                self._process_buffer()
            except TimeoutError:
                continue
            except Exception as e:
                self.logger.error(f"控制通道接收错误: {e}")
                break
        self._disconnect()
    
    def _process_buffer(self) -> None:
        """处理缓冲区"""
        while True:
            packet = self.buffer.pop_packet()
            if packet is None:
                break
            if self._on_packet:
                try:
                    self._on_packet(packet)
                except Exception as e:
                    self.logger.error(f"处理控制包错误: {e}")
    
    def send_packet(self, packet: Packet) -> bool:
        """发送数据包"""
        if not self._connected or not self.sock:
            return False
        try:
            with self._lock:
                self.sock.sendall(packet.to_bytes())
            return True
        except Exception as e:
            self.logger.error(f"控制通道发送错误: {e}")
            self._disconnect()
            return False
    
    def send_json(self, data: dict) -> bool:
        """发送JSON数据"""
        packet = Packet.create_json(2, data)  # type=2 for control
        return self.send_packet(packet)
    
    def set_on_packet(self, callback: Callable) -> None:
        """设置包接收回调"""
        self._on_packet = callback
    
    def _disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        self._stop = True
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                # 断开路径的清理失败不阻断；记录以便排查句柄泄漏。
                self.logger.debug("控制通道断开时关闭 socket 异常", exc_info=True)
            self.sock = None
        self.logger.info("控制通道已断开")
    
    def close(self) -> None:
        """关闭通道"""
        self._stop = True
        self._connected = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                # 主动关闭路径的清理失败不阻断；记录以便排查句柄泄漏。
                self.logger.debug("控制通道关闭时关闭 socket 异常", exc_info=True)
            self.sock = None
    
    def is_connected(self) -> bool:
        return self._connected
    
    def get_last_recv_time(self) -> float:
        """获取最后接收时间"""
        return self._last_recv