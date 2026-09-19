"""
被控端后台服务 - 主动推送模式
"""

import json
import socket
import ssl
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from rdc.capture.diff import ScreenDiffer
from rdc.capture.framerate import FramerateController
from rdc.capture.mss_source import ScreenCapturer
from rdc.client.ratelimit import RateLimiter
from rdc.config._v4_config import CONFIG_DIR, Config
from rdc.constants import (
    CONNECTION_RATE_LIMIT,
    DEFAULT_PORT,
    MAX_CONNECTIONS,
    PKT_TYPE_CLIPBOARD,
    PKT_TYPE_CONTROL,
    PKT_TYPE_HEARTBEAT,
    PKT_TYPE_SCREEN,
    PKT_TYPE_STREAM_CONTROL,
)
from rdc.encoding.jpeg import BandwidthProbe, JPEGEncoder
from rdc.logger import get_logger
from rdc.protocol.framing import Packet, PacketBuffer

from .pid_manager import PIDManager

# TODO(phase-6): v4 托盘基于 Tkinter + pystray，gui/ 已随原件移入 legacy_v4/，
# 故此导入在 v5 树中不存在，运行时走 except 分支 → HAS_TRAY=False（无托盘降级）。
# 阶段 6 将用 QSystemTrayIcon 重写为 rdc.ui.tray，届时替换本段并移除此 ignore。
# 用带错误码的 ignore（非裸 ignore），符合 pyproject 的 ignore-without-code 约束。
try:
    from gui.tray import SystemTray  # type: ignore[import-not-found]
    HAS_TRAY = True
except ImportError:
    # SystemTray 显式置 None，确保无论导入成败该名字都有定义（优于 v4 原样）。
    SystemTray = None
    HAS_TRAY = False


class RemoteService:
    """远程控制服务 - 主动推送画面"""

    def __init__(self, show_tray: bool = True):
        self.config = Config()
        self.logger = get_logger()
        self.pid_manager = PIDManager("RemoteControlService")
        self.show_tray = show_tray

        # 屏幕组件
        self.capturer = ScreenCapturer()
        self.encoder = JPEGEncoder()
        self.differ = ScreenDiffer()
        self.framerate = FramerateController(
            min_fps=self.config.get('min_fps', 5),
            max_fps=self.config.get('max_fps', 30)
        )

        # 网络
        self._server_socket: socket.socket | None = None
        self._control_socket: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._client_lock = threading.Lock()
        self._client_threads: dict[socket.socket, threading.Thread] = {}
        self._running = False          # 服务运行状态
        self._paused = False           # 是否暂停（服务停止但进程保持）
        self._should_exit = False      # 进程退出标志
        
        # 画面推送控制
        self._screen_streaming: dict[socket.socket, bool] = {}
        self._streaming_lock = threading.Lock()
        
        # 连接限流
        max_connections = self.config.get('max_connections', MAX_CONNECTIONS)
        self._connection_semaphore = threading.Semaphore(max_connections)
        self._rate_limiter = RateLimiter(
            max_connections_per_second=CONNECTION_RATE_LIMIT
        )
        
        # 线程池
        self._thread_pool = ThreadPoolExecutor(
            max_workers=max_connections * 2,
            thread_name_prefix="RemoteService"
        )
        
        # 统计信息
        # start_time 存储 time.time()（float），故整体标注为 float 值字典。
        self._stats: dict[str, float] = {
            'total_connections': 0,
            'active_connections': 0,
            'start_time': 0.0,
            'bytes_sent': 0,
            'bytes_recv': 0,
        }

        # ===== v4.0.0：ScreenBroadcaster 单例 =====
        # v4.0.1：注入 BandwidthProbe 实现动态码率调整
        self.bandwidth_probe = BandwidthProbe()
        self._broadcaster = self._ScreenBroadcaster(self, self.bandwidth_probe)

        # ===== v4.0.0：认证封禁持久化 =====
        self._banlist_path = CONFIG_DIR / "banlist.json"
        self._ban_timestamps: dict[str, float] = {}
        self._auth_failure_counts: dict[str, int] = {}
        self._load_banlist()

        # ===== v4.0.1：会话令牌（画面通道认证，60秒有效，单次使用） =====
        self._session_tokens: dict[str, float] = {}
        self._SESSION_TOKEN_TTL = 60.0

        # ===== v4.0.0：TLS =====
        self._ssl_context: ssl.SSLContext | None = None

        # 托盘
        self._tray: SystemTray | None = None
        self._tray_thread: threading.Thread | None = None

    def start(self) -> bool:
        """启动服务（如果已暂停则恢复）"""
        if self._should_exit:
            self.logger.warning("进程即将退出，无法启动服务")
            return False

        if self._running:
            self.logger.info("服务已在运行")
            return True

        self.logger.info("正在启动服务...")
        self.pid_manager.cleanup_stale()
        self._running = True
        self._paused = False
        self._stats['start_time'] = time.time()
        self.pid_manager.write()

        port = self.config.get('port', DEFAULT_PORT)
        control_port = self.config.get('control_port', DEFAULT_PORT - 1)

        # ===== v4.0.0：强制 TLS，自动生成自签名证书 =====
        self._ensure_tls_cert()
        self._create_ssl_context()

        if not self._start_server(port, control_port):
            self._running = False
            return False

        if self.show_tray and HAS_TRAY and self._tray is None:
            self._start_tray()

        self.logger.info(f"✅ 服务已启动，主端口: {port}，控制端口: {control_port}")
        self.logger.info(f"📡 画面主动推送模式，目标帧率: {self.config.get('max_fps', 30)} FPS")
        return True

    def _start_tray(self) -> None:
        """启动系统托盘"""
        def tray_loop():
            self._tray = SystemTray(
                on_toggle_pause=self._toggle_pause,
                on_restart=self._restart,
                on_exit=self.shutdown,
                is_paused_callback=self.is_paused
            )
            self._tray.set_client_count_callback(self.get_client_count)
            self._tray.run()

        self._tray_thread = threading.Thread(target=tray_loop, daemon=True)
        self._tray_thread.start()
        self.logger.info("系统托盘已启动")

    def _toggle_pause(self) -> None:
        """切换暂停/继续状态"""
        if self._paused:
            self.resume()
        else:
            self.pause()

    def pause(self) -> None:
        """暂停服务（进程保持，托盘保留）"""
        if not self._running or self._paused:
            return
        self.logger.info("正在暂停服务...")
        self._paused = True
        self._running = False   # 停止接受新连接和推送

        # 停止画面推送
        with self._streaming_lock:
            for conn in list(self._screen_streaming.keys()):
                self._screen_streaming[conn] = False
            self._screen_streaming.clear()

        # 关闭客户端（v4.0.0：强制shutdown中断阻塞recv）
        with self._client_lock:
            for conn in list(self._clients):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            self._clients.clear()
            self._client_threads.clear()

        # 停止广播器
        self._broadcaster.stop()

        # 关闭服务器 socket
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None
        if self._control_socket:
            try:
                self._control_socket.close()
            except Exception:
                pass
            self._control_socket = None

        # 关闭线程池（不等待，因为可能还有任务）
        try:
            self._thread_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        # PID文件保留，表示进程还在
        self.logger.info("✅ 服务已暂停（进程保持运行，托盘保留）")
        # 通知托盘更新菜单
        if self._tray:
            self._tray.update_menu()

    def resume(self) -> None:
        """恢复服务（从暂停状态）"""
        if not self._paused:
            return
        self.logger.info("正在恢复服务...")
        # 重新初始化网络组件
        self._running = True
        self._paused = False
        # 重新创建线程池
        max_connections = self.config.get('max_connections', MAX_CONNECTIONS)
        self._thread_pool = ThreadPoolExecutor(
            max_workers=max_connections * 2,
            thread_name_prefix="RemoteService"
        )
        port = self.config.get('port', DEFAULT_PORT)
        control_port = self.config.get('control_port', DEFAULT_PORT - 1)
        if self._start_server(port, control_port):
            self.logger.info("✅ 服务已恢复")
        else:
            self._running = False
            self._paused = True
            self.logger.error("❌ 服务恢复失败")
        # 通知托盘更新菜单
        if self._tray:
            self._tray.update_menu()

    def _restart(self) -> None:
        """重启服务（先完全停止再启动）"""
        self.logger.info("正在重启服务...")
        # 先暂停
        if not self._paused:
            self.pause()
        # 确保完全停止
        time.sleep(0.5)
        # 重新启动
        self._paused = False
        self.start()

    # ========== v4.0.0：TLS 证书管理 ==========

    def _ensure_tls_cert(self) -> None:
        """确保自签名TLS证书存在，不存在或过期则自动生成"""
        cert_path = CONFIG_DIR / "cert.pem"
        key_path = CONFIG_DIR / "key.pem"
        if cert_path.exists() and key_path.exists():
            try:
                import datetime

                from cryptography import x509
                cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                # 检查是否过期（剩余有效期不足7天则重新生成）
                try:
                    not_valid_after = cert.not_valid_after_utc
                except AttributeError:
                    not_valid_after = cert.not_valid_after.replace(tzinfo=datetime.UTC)
                if (not_valid_after - datetime.datetime.now(datetime.UTC)).days > 7:
                    return
            except Exception as e:
                self.logger.warning(f"证书检查失败，将重新生成: {e}")
        self._generate_self_signed_cert(cert_path, key_path)

    def _generate_self_signed_cert(self, cert_path, key_path) -> None:
        """使用cryptography库生成2048位RSA自签名证书"""
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "RemoteDesktop"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RemoteDesktop Control"),
        ])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()
        ))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.config.set('tls_cert_generated', int(time.time()))
        self.logger.info("🔐 已自动生成2048位RSA自签名TLS证书")

    def _create_ssl_context(self) -> None:
        """创建服务端SSL上下文（强制TLS 1.2+）"""
        cert_path = CONFIG_DIR / "cert.pem"
        key_path = CONFIG_DIR / "key.pem"
        self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        self.logger.info("🔐 TLS上下文已创建（强制TLS 1.2+）")

    def _start_server(self, port: int, control_port: int) -> bool:
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._server_socket.bind(('0.0.0.0', port))
            self._server_socket.listen(MAX_CONNECTIONS * 2)
            self._server_socket.settimeout(1.0)
            self.logger.info(f"📡 主端口监听: {port} (画面传输)")

            self._control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._control_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._control_socket.bind(('0.0.0.0', control_port))
            self._control_socket.listen(MAX_CONNECTIONS * 2)
            self._control_socket.settimeout(1.0)
            self.logger.info(f"📡 控制端口监听: {control_port} (控制指令)")

            threading.Thread(target=self._accept_loop, 
                           args=(self._server_socket, "主端口"), 
                           daemon=True).start()
            threading.Thread(target=self._accept_loop, 
                           args=(self._control_socket, "控制端口"), 
                           daemon=True).start()
            return True
            
        except OSError as e:
            self.logger.error(f"端口绑定失败: {e}")
            if self._server_socket:
                try:
                    self._server_socket.close()
                except:
                    pass
                self._server_socket = None
            if self._control_socket:
                try:
                    self._control_socket.close()
                except:
                    pass
                self._control_socket = None
            return False
        except Exception as e:
            self.logger.error(f"启动服务器失败: {e}")
            return False

    def _accept_loop(self, server_socket: socket.socket, socket_name: str = "主端口") -> None:
        # _accept_loop 由 _start_server 在 _create_ssl_context() 之后启动，
        # 运行时 self._ssl_context 必非 None；取局部引用收窄类型并做防御性判空。
        ssl_context = self._ssl_context
        if ssl_context is None:
            self.logger.error("TLS 上下文未初始化，无法接受连接")
            return
        while self._running:
            try:
                raw_conn, addr = server_socket.accept()

                # ===== v4.0.0：强制 TLS 包装 =====
                try:
                    conn = ssl_context.wrap_socket(raw_conn, server_side=True)
                except Exception as e:
                    self.logger.warning(f"TLS握手失败 {addr}: {e}")
                    try:
                        raw_conn.close()
                    except Exception:
                        pass
                    continue

                if not self._rate_limiter.allow():
                    conn.close()
                    self.logger.warning(f"{socket_name} 连接频率过高，拒绝 {addr}")
                    continue
                
                if not self._connection_semaphore.acquire(blocking=False):
                    conn.close()
                    self.logger.warning(f"{socket_name} 连接数过多，拒绝 {addr}")
                    continue
                
                self._stats['total_connections'] += 1
                self._stats['active_connections'] += 1
                self.logger.info(f"📶 {socket_name} 客户端连接: {addr} (活跃: {self._stats['active_connections']})")
                
                with self._client_lock:
                    self._clients.add(conn)
                
                self._thread_pool.submit(self._handle_client, conn, addr, socket_name)
                
            except TimeoutError:
                continue
            except Exception as e:
                if self._running:
                    self.logger.error(f"{socket_name} 接受连接错误: {e}")

    def _handle_client(self, conn: socket.socket, addr: tuple[str, int], socket_name: str = "主端口") -> None:
        buffer = PacketBuffer()
        client_addr = f"{addr[0]}:{addr[1]}"
        last_heartbeat = time.time()

        # ===== 新增：IP黑白名单检查 =====
        if not self._check_ip_whitelist(addr[0]):
            self.logger.warning(f"IP不在白名单: {client_addr}")
            conn.close()
            self._connection_semaphore.release()
            self._stats['active_connections'] -= 1
            return
        if self._is_ip_blacklisted(addr[0]):
            self.logger.warning(f"IP在黑名单: {client_addr}")
            conn.close()
            self._connection_semaphore.release()
            self._stats['active_connections'] -= 1
            return

        # ===== 认证流程（v4.0.1：控制端口密码认证+令牌，画面端口令牌认证） =====
        # v4.0.3：握手交换统一由 enable_auth 门控，保证两端结构严格对称：
        #   启用认证 → 控制端口必须交换认证消息并发放会话令牌，画面端口必须校验令牌；
        #   关闭认证 → 两端均不做任何交换。
        # 旧版服务端无条件发放令牌/无条件要求令牌，而客户端仅在配置了密码时才交换，
        # 导致未设密码时客户端拿不到令牌，画面通道认证必然失败、连接被服务端关闭（无画面）。
        is_control_port = (socket_name == "控制端口")
        enable_auth = self.config.get('enable_auth', False)

        if is_control_port:
            # 控制端口：密码认证
            if not self._authenticate_client(conn, addr):
                self.logger.warning(f"控制端口认证失败: {client_addr}")
                if enable_auth:
                    self._send_auth_response(conn, "AUTH_FAILED")
                conn.close()
                self._connection_semaphore.release()
                self._stats['active_connections'] -= 1
                return

            if enable_auth:
                # 认证成功：生成会话令牌（60秒有效，单次使用）
                token = str(uuid.uuid4())
                self._session_tokens[token] = time.time() + self._SESSION_TOKEN_TTL
                self._send_auth_response(conn, "AUTH_OK", token=token)
                self.logger.info(f"控制端口认证成功，已发放会话令牌: {client_addr}")
            else:
                self.logger.info(f"控制端口连接放行（未启用认证）: {client_addr}")
        else:
            # 画面端口：令牌认证（仅在启用认证时校验）
            if enable_auth:
                if not self._authenticate_screen_token(conn):
                    self.logger.warning(f"画面端口令牌认证失败: {client_addr}")
                    self._send_auth_response(conn, "AUTH_FAILED")
                    conn.close()
                    self._connection_semaphore.release()
                    self._stats['active_connections'] -= 1
                    return
                self._send_auth_response(conn, "AUTH_OK")
                self.logger.info(f"画面端口令牌认证成功: {client_addr}")
            else:
                self.logger.info(f"画面端口连接放行（未启用认证）: {client_addr}")
        # ===== 认证结束 =====

        with self._streaming_lock:
            self._screen_streaming[conn] = False

        try:
            conn.settimeout(10.0)
            self._send_welcome(conn)

            while self._running:
                try:
                    data = conn.recv(65536)
                    if not data:
                        self.logger.info(f"客户端断开: {client_addr}")
                        break

                    # v4.0.3 修复：旧版 last_heartbeat 仅在连接建立时赋值、从不更新，
                    # 导致任何一次超过10秒的 recv 超时都会直接满足“>20秒”条件，
                    # 控制通道被服务端立即断开（客户端表现为连接一段时间后卡死）。
                    # 现改为收到任何客户端数据即视为活跃。
                    last_heartbeat = time.time()

                    self._stats['bytes_recv'] += len(data)
                    buffer.append(data)
                    self._process_packets(conn, buffer, client_addr)

                except TimeoutError:
                    # v4.0.3 修复：画面流是纯下行通道，客户端订阅后不再发送上行
                    # 数据，旧版以 recv 超时判活会在20秒后必然误断画面通道，
                    # 导致主控端连接一会儿就黑屏。只要该连接仍是活跃订阅者就
                    # 视为存活；若连接真正死亡，广播器 sendall 失败时会移除订阅，
                    # 随后此超时机制会在20秒后正常清理。
                    if self._broadcaster.has_subscriber(conn):
                        last_heartbeat = time.time()
                        continue
                    if time.time() - last_heartbeat > 20:
                        self.logger.warning(f"客户端心跳超时: {client_addr}")
                        break
                    continue
                except OSError as e:
                    self.logger.error(f"Socket错误 {client_addr}: {e}")
                    break

        except Exception as e:
            self.logger.error(f"处理客户端错误 {client_addr}: {e}")
        finally:
            # v4.0.0：从广播器注销订阅
            self._broadcaster.remove_subscriber(conn)
            with self._streaming_lock:
                self._screen_streaming.pop(conn, None)
            with self._client_lock:
                self._clients.discard(conn)
                self._client_threads.pop(conn, None)
            self._connection_semaphore.release()
            self._stats['active_connections'] -= 1
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self.logger.info(f"客户端已清理: {client_addr} (活跃: {self._stats['active_connections']})")
            if self._tray:
                self._tray.update_client_count(self.get_client_count())

    def _send_welcome(self, conn: socket.socket) -> None:
        try:
            welcome = Packet.create_json(PKT_TYPE_CONTROL, {
                'type': 'welcome',
                'version': '4.0.0',
                'timestamp': time.time()
            })
            conn.sendall(welcome.to_bytes())
            self._stats['bytes_sent'] += len(welcome.to_bytes())
        except Exception as e:
            self.logger.error(f"发送欢迎信息失败: {e}")

    def _process_packets(self, conn: socket.socket, buffer: PacketBuffer, client_addr: str) -> None:
        while True:
            packet = buffer.pop_packet()
            if packet is None:
                break
            try:
                if packet.type == PKT_TYPE_STREAM_CONTROL:
                    self._handle_stream_control(conn, packet)
                elif packet.type == PKT_TYPE_SCREEN:
                    # 旧的请求模式（兼容）
                    self._start_screen_streaming(conn)
                elif packet.type == PKT_TYPE_CONTROL:
                    self._handle_control(conn, packet)
                elif packet.type == PKT_TYPE_HEARTBEAT:
                    self._handle_heartbeat(conn, packet)
                elif packet.type == PKT_TYPE_CLIPBOARD:
                    self._handle_clipboard(packet)
                else:
                    self.logger.debug(f"未知包类型: {packet.type}")
            except Exception as e:
                self.logger.error(f"处理数据包错误: {e}")

    def _start_screen_streaming(self, conn: socket.socket) -> None:
        """启动画面推送（v4.0.0：注册到ScreenBroadcaster单例，Zero-Copy分发）"""
        with self._streaming_lock:
            if self._screen_streaming.get(conn, False):
                return
            self._screen_streaming[conn] = True

        self.logger.info(f"📷 注册画面订阅者 (客户端: {conn.getpeername()})")
        self._broadcaster.add_subscriber(conn)

    def _stop_screen_streaming(self, conn: socket.socket) -> None:
        """停止画面推送（v4.0.0：从ScreenBroadcaster注销）"""
        with self._streaming_lock:
            if conn in self._screen_streaming:
                self._screen_streaming[conn] = False
        self._broadcaster.remove_subscriber(conn)
        self.logger.info(f"📷 注销画面订阅者 (客户端: {conn.getpeername()})")

    def _handle_stream_control(self, conn: socket.socket, packet: Packet) -> None:
        """处理流控制指令"""
        import json
        try:
            data = json.loads(packet.payload.decode('utf-8'))
            cmd = data.get('cmd', '')

            if cmd == 'start':
                # 更新配置
                config = data.get('config', {})
                if config:
                    self._update_stream_config(config)
                self._start_screen_streaming(conn)

            elif cmd == 'stop':
                self._stop_screen_streaming(conn)

            elif cmd == 'config':
                # 仅更新配置，不启停流
                config = data.get('config', {})
                if config:
                    self._update_stream_config(config)

        except Exception as e:
            self.logger.error(f"处理流控制失败: {e}")

    def _update_stream_config(self, config: dict) -> None:
        """更新流配置"""
        if 'quality' in config:
            self.encoder.set_quality(config['quality'])
        if 'max_fps' in config:
            self.framerate.max_fps = config['max_fps']
        if 'min_fps' in config:
            self.framerate.min_fps = config['min_fps']
        self.logger.debug(f"流配置已更新: {config}")

    # ========== 认证与安全 ==========

    def _check_ip_whitelist(self, ip: str) -> bool:
        """检查IP是否在白名单"""
        if not self.config.get('enable_whitelist', False):
            return True
        whitelist = self.config.get('whitelist_ips', [])
        return ip in whitelist

    def _is_ip_blacklisted(self, ip: str) -> bool:
        """检查IP是否在黑名单"""
        if not self.config.get('enable_blacklist', False):
            return False
        blacklist = self.config.get('blacklist_ips', [])
        return ip in blacklist

    def _authenticate_client(self, conn: socket.socket, addr: tuple[str, int]) -> bool:
        """认证客户端（v4.0.0：Bcrypt验证 + 封禁持久化 + 自动解封）"""
        import json
        import struct

        enable_auth = self.config.get('enable_auth', False)
        if not enable_auth:
            return True

        # v4.0.3：启用认证时，即使未设置密码也必须读取客户端的认证消息
        # （客户端此时会发送空密码消息），保持握手对称；未设密码时
        # verify_password 内部会直接放行。旧版在此提前返回不读消息，
        # 导致客户端发来的消息残留在流中。
        # 注：v4 此处有 `password_hash = self.config.get(...)` 的死赋值，真实哈希
        # 由 verify_password 内部读取，故删除；阶段 2 迁移到 SecretStore 后统一处理。

        ip = addr[0]
        ban_duration = self.config.get('auth_ban_duration', 300)

        # 检查是否被封禁（持久化，服务重启不丢失）
        self._cleanup_expired_bans()
        if ip in self._ban_timestamps:
            if time.time() - self._ban_timestamps[ip] < ban_duration:
                self.logger.warning(f"IP {ip} 已被封禁，拒绝认证")
                return False
            else:
                # 自动解封
                del self._ban_timestamps[ip]
                self._auth_failure_counts.pop(ip, None)
                self._save_banlist()

        try:
            conn.settimeout(self.config.get('auth_timeout', 10))

            # 批量读取：先读4字节长度前缀
            length_data = conn.recv(4)
            if len(length_data) < 4:
                self._record_auth_failure(ip)
                return False

            length = struct.unpack('>I', length_data)[0]
            if length > 4096:  # 防止攻击
                self._record_auth_failure(ip)
                return False

            # 读取实际数据
            data = b''
            while len(data) < length:
                chunk = conn.recv(min(4096, length - len(data)))
                if not chunk:
                    break
                data += chunk

            if len(data) < length:
                self._record_auth_failure(ip)
                return False

            # 解析JSON
            try:
                auth_data = json.loads(data.decode('utf-8'))
                password = auth_data.get('password', '')
            except Exception:
                self._record_auth_failure(ip)
                return False

            # v4.0.0：使用Bcrypt验证密码
            if self.config.verify_password(password):
                # 认证成功，清除失败计数
                self._auth_failure_counts.pop(ip, None)
                return True
            else:
                self._record_auth_failure(ip)
                return False

        except TimeoutError:
            self._record_auth_failure(ip)
            return False
        except Exception as e:
            self.logger.error(f"认证异常: {e}")
            return False
        finally:
            conn.settimeout(None)

    def _record_auth_failure(self, ip: str) -> None:
        """记录认证失败（v4.0.0：持久化到banlist.json）"""
        self._auth_failure_counts[ip] = self._auth_failure_counts.get(ip, 0) + 1
        if self._auth_failure_counts[ip] >= self.config.get('max_auth_attempts', 3):
            self._ban_timestamps[ip] = time.time()
            self._save_banlist()
            self.logger.warning(f"IP {ip} 已被封禁 {self.config.get('auth_ban_duration', 300)} 秒")

    def _cleanup_expired_bans(self) -> None:
        """清理过期的封禁记录（自动解封）"""
        ban_duration = self.config.get('auth_ban_duration', 300)
        now = time.time()
        expired = [ip for ip, ts in self._ban_timestamps.items() if now - ts >= ban_duration]
        for ip in expired:
            del self._ban_timestamps[ip]
            self._auth_failure_counts.pop(ip, None)
        if expired:
            self._save_banlist()
            self.logger.info(f"自动解封 {len(expired)} 个IP")

    def _load_banlist(self) -> None:
        """从banlist.json加载封禁记录"""
        try:
            if self._banlist_path.exists():
                with open(self._banlist_path, encoding='utf-8') as f:
                    data = json.load(f)
                self._ban_timestamps = data.get('ban_timestamps', {})
                self._auth_failure_counts = data.get('failure_counts', {})
        except Exception as e:
            self.logger.warning(f"加载banlist失败: {e}")
            self._ban_timestamps = {}
            self._auth_failure_counts = {}

    def _save_banlist(self) -> None:
        """保存封禁记录到banlist.json（v4.0.1：最大1000条，超出裁剪最早记录）"""
        try:
            # v4.0.1：防止文件无限增长，最多保留1000条
            MAX_BAN_RECORDS = 1000
            if len(self._ban_timestamps) > MAX_BAN_RECORDS:
                # 按时间戳排序，保留最新的1000条
                sorted_bans = sorted(self._ban_timestamps.items(), key=lambda x: x[1], reverse=True)
                keep_ips = {ip for ip, _ in sorted_bans[:MAX_BAN_RECORDS]}
                self._ban_timestamps = {ip: ts for ip, ts in self._ban_timestamps.items() if ip in keep_ips}
                self._auth_failure_counts = {ip: c for ip, c in self._auth_failure_counts.items() if ip in keep_ips}
                self.logger.info(f"banlist已裁剪至{MAX_BAN_RECORDS}条")

            self._banlist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._banlist_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'ban_timestamps': self._ban_timestamps,
                    'failure_counts': self._auth_failure_counts,
                    'saved_at': time.time(),
                }, f, indent=2)
        except Exception as e:
            self.logger.warning(f"保存banlist失败: {e}")

    # ========== v4.0.1：会话令牌认证（画面通道） ==========

    def _send_auth_response(self, conn: socket.socket, status: str, token: str | None = None) -> None:
        """发送长度前缀的JSON认证响应（v4.0.1）"""
        payload = {'status': status}
        if token:
            payload['token'] = token
        data = json.dumps(payload).encode('utf-8')
        try:
            conn.sendall(struct.pack('>I', len(data)) + data)
        except Exception:
            pass

    def _cleanup_expired_tokens(self) -> None:
        """清理过期的会话令牌"""
        now = time.time()
        expired = [t for t, exp in self._session_tokens.items() if now >= exp]
        for t in expired:
            del self._session_tokens[t]

    def _authenticate_screen_token(self, conn: socket.socket) -> bool:
        """画面通道令牌认证（v4.0.1）

        读取4字节长度前缀 + JSON {"token": "..."}，
        校验令牌存在、未过期、未被使用。使用后立即失效（防重放）。
        """
        try:
            conn.settimeout(self.config.get('auth_timeout', 10))

            # 读取4字节长度前缀
            length_data = conn.recv(4)
            if len(length_data) < 4:
                return False
            length = struct.unpack('>I', length_data)[0]
            if length > 4096:
                return False

            # 读取JSON payload
            data = b''
            while len(data) < length:
                chunk = conn.recv(min(4096, length - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) < length:
                return False

            auth_data = json.loads(data.decode('utf-8'))
            token = auth_data.get('token', '')

            if not token:
                return False

            # 清理过期令牌后校验
            self._cleanup_expired_tokens()
            if token not in self._session_tokens:
                self.logger.warning(f"画面通道令牌无效或已过期: {token[:8]}...")
                return False

            # 单次使用：验证后立即删除，防止重放
            del self._session_tokens[token]
            return True

        except (TimeoutError, json.JSONDecodeError, struct.error) as e:
            self.logger.warning(f"画面通道令牌认证异常: {e}")
            return False
        except Exception as e:
            self.logger.error(f"画面通道令牌认证错误: {e}")
            return False
        finally:
            conn.settimeout(None)

    def _handle_control(self, conn: socket.socket, packet: Packet) -> None:
        try:
            data = packet.to_json()
            cmd_type = data.get('type', '')
            if cmd_type == 'mouse':
                self._execute_mouse(data)
            elif cmd_type == 'key':
                self._execute_key(data)
            elif cmd_type == 'ping':
                pong = Packet.create_json(PKT_TYPE_HEARTBEAT, {
                    'type': 'pong',
                    'timestamp': time.time()
                })
                conn.sendall(pong.to_bytes())
                self._stats['bytes_sent'] += len(pong.to_bytes())
            else:
                self.logger.debug(f"未知控制指令: {cmd_type}")
        except Exception as e:
            self.logger.error(f"处理控制指令失败: {e}")

    def _execute_mouse(self, data: dict) -> None:
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            action = data.get('action', 'move')
            x = data.get('x', 0)
            y = data.get('y', 0)
            button = data.get('button', 'left')
            screen_w, screen_h = self.capturer.get_screen_size()
            x = max(0, min(screen_w - 1, x))
            y = max(0, min(screen_h - 1, y))
            if action == 'move':
                pyautogui.moveTo(x, y)
            elif action == 'press':
                pyautogui.mouseDown(x, y, button=button)
            elif action == 'release':
                pyautogui.mouseUp(button=button)
            elif action == 'click':
                pyautogui.click(x, y, button=button)
            elif action == 'scroll':
                delta = data.get('delta', 0)
                if delta != 0:
                    pyautogui.scroll(delta)
        except Exception as e:
            self.logger.error(f"执行鼠标命令失败: {e}")

    def _execute_key(self, data: dict) -> None:
        try:
            import pyautogui
            action = data.get('action', 'press')
            key = data.get('key', '')
            modifiers = data.get('modifiers', [])
            if action == 'press':
                for mod in modifiers:
                    pyautogui.keyDown(mod)
                pyautogui.keyDown(key)
                for mod in reversed(modifiers):
                    pyautogui.keyUp(mod)
            elif action == 'release':
                pyautogui.keyUp(key)
            elif action == 'type':
                text = data.get('text', '')
                pyautogui.typewrite(text)
        except Exception as e:
            self.logger.error(f"执行键盘命令失败: {e}")

    def _handle_heartbeat(self, conn: socket.socket, packet: Packet) -> None:
        try:
            data = packet.to_json()
            if data.get('type') == 'ping':
                pong = Packet.create_json(PKT_TYPE_HEARTBEAT, {
                    'type': 'pong',
                    'timestamp': time.time()
                })
                conn.sendall(pong.to_bytes())
                self._stats['bytes_sent'] += len(pong.to_bytes())
        except Exception as e:
            self.logger.error(f"处理心跳失败: {e}")

    def _handle_clipboard(self, packet: Packet) -> None:
        try:
            data = packet.to_json()
            text = data.get('text', '')
            if text:
                import tkinter as tk
                root = tk.Tk()
                root.withdraw()
                root.clipboard_clear()
                root.clipboard_append(text)
                root.update()
                root.destroy()
                self.logger.debug(f"剪贴板已更新: {text[:50]}...")
        except Exception as e:
            self.logger.error(f"处理剪贴板失败: {e}")

    # ========== 外部控制接口 ==========
    def is_running(self) -> bool:
        return self._running

    def is_paused(self) -> bool:
        return self._paused

    def should_exit(self) -> bool:
        return self._should_exit

    def shutdown(self) -> None:
        """完全退出（进程终止，托盘销毁）"""
        self.logger.info("正在完全关闭服务...")
        self._should_exit = True
        self._running = False
        self._paused = False

        with self._streaming_lock:
            for conn in list(self._screen_streaming.keys()):
                self._screen_streaming[conn] = False
            self._screen_streaming.clear()

        with self._client_lock:
            for conn in list(self._clients):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            self._clients.clear()
            self._client_threads.clear()

        # 停止广播器
        self._broadcaster.stop()

        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None
        if self._control_socket:
            try:
                self._control_socket.close()
            except Exception:
                pass
            self._control_socket = None

        try:
            self._thread_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        self.pid_manager.remove()
        self.logger.info("✅ 服务已完全关闭，进程即将退出")

    def get_client_count(self) -> int:
        with self._client_lock:
            return len(self._clients)

    def get_stats(self) -> dict:
        uptime = time.time() - self._stats['start_time'] if self._stats['start_time'] else 0
        return {
            **self._stats,
            'uptime': uptime,
            'active_connections': self.get_client_count(),
            'rate_limiter_count': self._rate_limiter.get_connection_count(),
            # ===== 编码统计（来自ScreenBroadcaster） =====
            'encoder_stats': self._broadcaster.get_stats(),
            'subscriber_count': self._broadcaster.subscriber_count(),
        }

    # ========== v4.0.0：ScreenBroadcaster 单例（Zero-Copy 画面广播） ==========

    class _ScreenBroadcaster:
        """
        全局单例画面广播器。
        仅维护1个截图→差分→编码线程，编码后的帧通过引用计数
        分发给所有活跃订阅客户端（Zero-Copy），解决多客户端CPU雪崩。
        """

        def __init__(self, service: 'RemoteService', bandwidth_probe: BandwidthProbe):
            self.service = service
            self.logger = service.logger
            self.bandwidth_probe = bandwidth_probe  # v4.0.1
            self._subscribers: dict[socket.socket, dict] = {}
            self._sub_lock = threading.Lock()
            self._running = False
            self._thread: threading.Thread | None = None
            self._consecutive_failures = 0  # v4.0.1：连续失败计数器
            self._quality_adjust_counter = 0  # v4.0.1：质量调整计数
            # 编码统计
            # avg_size 为浮点均值，故整体标注为 float 值字典。
            self._stats: dict[str, float] = {
                'frames_encoded': 0,
                'current_quality': 0,
                'avg_size': 0.0,
                'total_size': 0,
            }

        def add_subscriber(self, conn: socket.socket) -> None:
            """注册订阅者；首个订阅者启动广播线程"""
            with self._sub_lock:
                if conn not in self._subscribers:
                    self._subscribers[conn] = {}
                    self.logger.info(f"📡 新增画面订阅者，当前: {len(self._subscribers)}")
            if not self._running:
                self.start()

        def remove_subscriber(self, conn: socket.socket) -> None:
            """注销订阅者；最后一个订阅者离开时停止广播线程"""
            with self._sub_lock:
                if conn in self._subscribers:
                    del self._subscribers[conn]
                    self.logger.info(f"📡 移除画面订阅者，当前: {len(self._subscribers)}")
            if not self._subscribers and self._running:
                self.stop()

        def start(self) -> None:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._broadcast_loop, daemon=True, name="ScreenBroadcaster")
            self._thread.start()
            self.logger.info("📡 ScreenBroadcaster 广播线程已启动")

        def stop(self) -> None:
            self._running = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)
            self._thread = None

        def subscriber_count(self) -> int:
            with self._sub_lock:
                return len(self._subscribers)

        def has_subscriber(self, conn: socket.socket) -> bool:
            """v4.0.3：判断连接是否仍在订阅画面流（供处理线程存活判定）"""
            with self._sub_lock:
                return conn in self._subscribers

        def get_stats(self) -> dict:
            return dict(self._stats)

        def _broadcast_loop(self) -> None:
            """全局唯一的截图→差分→编码→分发循环（v4.0.1：重试+带宽探测）"""
            target_fps = self.service.config.get('max_fps', 30)
            frame_interval = 1.0 / target_fps if target_fps > 0 else 0.033

            while self._running:
                try:
                    frame_start = time.time()

                    # 截图
                    img = self.service.capturer.capture()
                    changed_blocks = self.service.differ.get_changed_blocks(img)
                    screen_w, screen_h = self.service.capturer.get_screen_size()
                    use_compression = self.service.config.get('enable_compression', True)

                    # 差分/全帧编码
                    if changed_blocks is None or len(changed_blocks) > 100:
                        img_data, quality, fmt = self.service.encoder.encode(img)
                        packet = Packet.create_screen(
                            img_data, screen_w, screen_h,
                            quality, is_diff=False,
                            offset_x=0, offset_y=0,
                            compress=use_compression
                        )
                    else:
                        cropped, (offset_x, offset_y) = self.service.differ.get_changed_region(img, changed_blocks)
                        img_data, quality, fmt = self.service.encoder.encode(cropped)
                        packet = Packet.create_screen(
                            img_data, screen_w, screen_h,
                            quality, is_diff=True,
                            offset_x=offset_x, offset_y=offset_y,
                            compress=use_compression
                        )

                    # Zero-Copy 分发：同一帧字节引用发送给所有订阅者
                    frame_bytes = packet.to_bytes()
                    self._stats['frames_encoded'] += 1
                    self._stats['current_quality'] = quality
                    self._stats['total_size'] += len(frame_bytes)
                    self._stats['avg_size'] = self._stats['total_size'] / self._stats['frames_encoded']

                    # v4.0.1：记录带宽探测
                    self.bandwidth_probe.record_send(len(frame_bytes))

                    # v4.0.1：每30帧根据带宽动态调整编码质量
                    self._quality_adjust_counter += 1
                    if self._quality_adjust_counter >= 30:
                        self._quality_adjust_counter = 0
                        try:
                            self.service.encoder.adjust_quality_by_bandwidth(
                                self.bandwidth_probe, target_fps
                            )
                        except Exception as e:
                            self.logger.debug(f"动态质量调整失败: {e}")

                    with self._sub_lock:
                        dead_conns = []
                        for conn in list(self._subscribers.keys()):
                            try:
                                conn.sendall(frame_bytes)
                                self.service._stats['bytes_sent'] += len(frame_bytes)
                            except Exception:
                                dead_conns.append(conn)
                        for conn in dead_conns:
                            self._subscribers.pop(conn, None)

                    # v4.0.1：成功后重置失败计数器
                    self._consecutive_failures = 0

                    elapsed = time.time() - frame_start
                    if elapsed < frame_interval:
                        time.sleep(frame_interval - elapsed)
                    elif elapsed > frame_interval * 1.5:
                        self.logger.debug(f"广播帧处理超时: {elapsed*1000:.0f}ms")

                except Exception as e:
                    self._consecutive_failures += 1
                    self.logger.error(f"ScreenBroadcaster 广播错误(连续{self._consecutive_failures}次): {e}")
                    # v4.0.1：连续失败超过5次，尝试重新初始化截图器
                    if self._consecutive_failures > 5:
                        try:
                            self.logger.warning("连续截图失败，尝试重新初始化ScreenCapturer")
                            self.service.capturer = ScreenCapturer()
                            # v4.0.3：同步重置差分缓存，否则旧帧尺寸与新截图不匹配，
                            # 会导致后续帧持续异常
                            self.service.differ.reset()
                            self._consecutive_failures = 0
                            time.sleep(0.5)
                        except Exception as reinit_err:
                            self.logger.error(f"重新初始化ScreenCapturer失败: {reinit_err}")
                            # 重新初始化仍失败，暂停5秒避免CPU空转
                            time.sleep(5.0)
                    else:
                        time.sleep(0.05)