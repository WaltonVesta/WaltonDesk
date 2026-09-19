"""
画面传输通道
独立Socket，大包传输，支持流控
"""

import socket
import struct
import ssl
import json
import threading
import time
from typing import Optional, Callable
from collections import deque

from constants import RECV_BUFFER_SIZE, SEND_QUEUE_SIZE
from core.protocol import Packet, PacketBuffer
from logger import get_logger


class ScreenChannel:
    """画面传输通道"""
    
    def __init__(self):
        self.sock: Optional[socket.socket] = None
        self.buffer = PacketBuffer()
        self.send_queue = deque(maxlen=SEND_QUEUE_SIZE)
        self._connected = False
        self._stop = False
        self._recv_thread: Optional[threading.Thread] = None
        self._send_thread: Optional[threading.Thread] = None
        self._on_packet: Optional[Callable] = None
        self.logger = get_logger()
        self._lock = threading.Lock()
        self._send_event = threading.Event()
        self._frame_count = 0
        self._last_frame_time = time.time()
        self._frame_interval = 1.0 / 30  # 默认30fps
        self._tls_context: Optional[ssl.SSLContext] = None
        self._host: str = ""
        self._session_token: Optional[str] = None  # v4.0.1：会话令牌
        self._on_disconnect: Optional[Callable] = None  # v4.0.3：意外断开回调
        self._disconnect_notified = False  # v4.0.3：防止重复通知
    
    def connect(self, host: str, port: int) -> bool:
        """连接（v4.0.0：支持TLS包装）"""
        try:
            is_ipv6 = ':' in host
            family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
            raw_sock = socket.socket(family, socket.SOCK_STREAM)
            raw_sock.settimeout(5.0)
            raw_sock.connect((host, port))

            # ===== TLS 包装（如果已设置上下文） =====
            if self._tls_context is not None:
                self.sock = self._tls_context.wrap_socket(raw_sock, server_hostname=host)
            else:
                self.sock = raw_sock

            self.sock.settimeout(None)
            # 开启TCP_NODELAY
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 设置发送缓冲区
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            # 设置接收缓冲区
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._connected = True
            self._stop = False
            self._host = host
            self._disconnect_notified = False  # v4.0.3：重置断开通知标志

            # v4.0.1：画面通道令牌认证（发送会话令牌）
            if self._session_token:
                if not self._send_session_token():
                    self.logger.error("画面通道令牌认证失败")
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                    self._connected = False
                    return False

            self.logger.info(f"画面通道已连接: {host}:{port}")
            self._start_threads()
            return True
        except Exception as e:
            self.logger.error(f"画面通道连接失败: {e}")
            return False

    def set_tls_context(self, context: ssl.SSLContext) -> None:
        """设置TLS上下文（由控制通道传入，复用证书信任配置）"""
        self._tls_context = context

    def set_session_token(self, token: str) -> None:
        """v4.0.1：设置会话令牌（由控制通道认证后获取）"""
        self._session_token = token

    def _send_session_token(self) -> bool:
        """v4.0.1：发送会话令牌并读取认证响应"""
        import struct
        try:
            auth_data = json.dumps({'token': self._session_token}).encode('utf-8')
            self.sock.sendall(struct.pack('>I', len(auth_data)) + auth_data)

            # 读取4字节长度前缀响应
            self.sock.settimeout(5.0)
            length_data = b''
            while len(length_data) < 4:
                chunk = self.sock.recv(4 - len(length_data))
                if not chunk:
                    return False
                length_data += chunk
            length = struct.unpack('>I', length_data)[0]
            if length > 4096:
                return False
            resp_data = b''
            while len(resp_data) < length:
                chunk = self.sock.recv(min(4096, length - len(resp_data)))
                if not chunk:
                    break
                resp_data += chunk
            self.sock.settimeout(None)

            try:
                resp_json = json.loads(resp_data.decode('utf-8'))
                return resp_json.get('status') == 'AUTH_OK'
            except (json.JSONDecodeError, UnicodeDecodeError):
                return resp_data == b'AUTH_OK'
        except Exception as e:
            self.logger.error(f"画面通道令牌认证异常: {e}")
            return False
    
    def set_target_fps(self, fps: int) -> None:
        """设置目标帧率"""
        self._frame_interval = 1.0 / fps if fps > 0 else 0.033

    def _start_threads(self) -> None:
        """启动收发线程"""
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._send_thread.start()
    
    def _recv_loop(self) -> None:
        """接收循环（优化版）"""
        while self._connected and not self._stop:
            try:
                # 使用非阻塞方式，增加超时
                if self.sock:
                    self.sock.settimeout(0.1)
                    data = self.sock.recv(RECV_BUFFER_SIZE)
                    if not data:
                        self.logger.warning("画面通道接收断开")
                        break
                    self.buffer.append(data)
                    self._process_buffer()

                    # FPS统计
                    now = time.time()
                    if now - self._last_frame_time >= 1.0:
                        self.logger.debug(f"画面接收速率: {self._frame_count} fps")
                        self._frame_count = 0
                        self._last_frame_time = now

            except socket.timeout:
                continue
            except Exception as e:
                self.logger.error(f"画面通道接收错误: {e}")
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
                    self.logger.error(f"处理画面包错误: {e}")
    
    def _send_loop(self) -> None:
        """发送循环（带流控）"""
        while self._connected and not self._stop:
            self._send_event.wait(timeout=0.1)
            if self._stop:
                break
            
            # 取队列中的包发送
            sent_count = 0
            while self.send_queue and sent_count < 5:  # 每批最多5帧
                try:
                    packet = self.send_queue.popleft()
                    self.sock.sendall(packet.to_bytes())
                    sent_count += 1
                    self._frame_count += 1
                except Exception as e:
                    self.logger.error(f"画面通道发送错误: {e}")
                    self._disconnect()
                    break
            self._send_event.clear()
    
    def send(self, packet: Packet) -> bool:
        """发送数据包（非阻塞，队列化）"""
        if not self._connected:
            return False
        
        # 流控：队列满时丢弃最旧的帧
        if len(self.send_queue) >= SEND_QUEUE_SIZE:
            try:
                dropped = self.send_queue.popleft()
                self.logger.debug("丢弃过时画面帧")
            except IndexError:
                pass
        
        self.send_queue.append(packet)
        self._send_event.set()
        return True
    
    def set_on_packet(self, callback: Callable) -> None:
        """设置包接收回调"""
        self._on_packet = callback

    def set_on_disconnect(self, callback: Optional[Callable]) -> None:
        """v4.0.3：设置断开回调（连接意外断开时调用一次）"""
        self._on_disconnect = callback
    
    def _disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        self._stop = True
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        self.logger.info("画面通道已断开")
        # v4.0.3：通知上层意外断开（幂等，防止收发线程并发重复触发）
        if self._on_disconnect and not self._disconnect_notified:
            self._disconnect_notified = True
            try:
                self._on_disconnect()
            except Exception as e:
                self.logger.error(f"画面通道断开回调异常: {e}")
    
    def close(self) -> None:
        """关闭通道"""
        self._stop = True
        self._connected = False
        self._disconnect_notified = True  # v4.0.3：主动关闭不触发断开回调
        self._send_event.set()
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
    
    def is_connected(self) -> bool:
        return self._connected
    
    def get_frame_count(self) -> int:
        """获取发送帧数"""
        return self._frame_count
    
    def get_queue_size(self) -> int:
        """获取队列大小"""
        return len(self.send_queue)