"""
KCP 协议通道（抗丢包）
需要安装: pip install kcp-python
"""
import socket
import threading
from typing import Optional, Callable

from logger import get_logger

try:
    import kcp
    HAS_KCP = True
except ImportError:
    HAS_KCP = False


class KCPChannel:
    """KCP 可靠传输通道"""

    def __init__(self):
        self.sock: Optional[socket.socket] = None
        self.kcp: Optional['kcp.KCP'] = None
        self._connected = False
        self._on_packet: Optional[Callable] = None
        self._recv_buffer = bytearray()
        self.logger = get_logger()

    def connect(self, host: str, port: int, conv: int = 12345) -> bool:
        """连接（UDP + KCP）"""
        if not HAS_KCP:
            self.logger.error("KCP 未安装，请执行 pip install kcp-python")
            return False

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.connect((host, port))

            # 初始化 KCP
            self.kcp = kcp.KCP(conv, self._udp_output)
            self.kcp.wndsize(128, 128)  # 窗口大小
            self.kcp.nodelay(1, 20, 2, 1)  # 快速模式

            self._connected = True
            threading.Thread(target=self._recv_loop, daemon=True).start()
            return True

        except Exception as e:
            self.logger.error(f"KCP连接失败: {e}")
            return False

    def _udp_output(self, data: bytes) -> None:
        """KCP 输出回调"""
        if self.sock:
            self.sock.sendto(data, self.sock.getpeername())

    def _recv_loop(self) -> None:
        """接收循环"""
        while self._connected and self.sock:
            try:
                data, _ = self.sock.recvfrom(65535)
                self.kcp.input(data)

                # 读取所有可用数据
                while True:
                    recv_data = self.kcp.recv(65535)
                    if not recv_data:
                        break
                    if self._on_packet:
                        self._on_packet(recv_data)

            except Exception as e:
                self.logger.error(f"KCP接收错误: {e}")
                break

    def send(self, data: bytes) -> bool:
        """发送数据"""
        if not self._connected or not self.kcp:
            return False
        try:
            self.kcp.send(data)
            return True
        except Exception as e:
            self.logger.error(f"KCP发送错误: {e}")
            return False

    def set_on_packet(self, callback: Callable) -> None:
        """设置包接收回调"""
        self._on_packet = callback

    def close(self) -> None:
        """关闭通道"""
        self._connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.kcp = None

    def is_connected(self) -> bool:
        return self._connected
