"""
剪贴板同步（v4.0.1 优化版）
强制依赖 pyperclip，使用主线程定时器（root.after）驱动轮询，
pyperclip.paste()/copy() 放入线程池执行，避免阻塞主线程 UI。
"""

import tkinter as tk
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pyperclip  # v4.0.0：强制依赖，不再可选

from logger import get_logger


class ClipboardManager:
    """剪贴板管理器"""

    def __init__(self, controller):
        self.controller = controller
        self.logger = get_logger()
        self._last_text = ""
        self._sync_enabled = True
        self._remote_text = ""
        self._running = True
        self._lock = threading.Lock()
        self._pending_chunks = {}
        self._chunk_start_time = 0
        self._chunk_timeout = 5.0
        self._poll_after_id = None

        # v4.0.1：线程池执行 pyperclip 调用，避免阻塞主线程
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ClipboardIO")

        # 由主线程 after 循环驱动轮询
        self.start_polling()

    def start_polling(self):
        """启动主线程轮询（root.after 驱动，100ms 间隔）"""
        if self._poll_after_id is not None:
            return
        try:
            root = self.controller.root
            self._poll_after_id = root.after(100, self._poll_once)
            self.logger.debug("剪贴板主线程轮询已启动")
        except Exception as e:
            self.logger.warning(f"启动剪贴板轮询失败: {e}")

    def _poll_once(self):
        """单次轮询调度（v4.0.1：pyperclip.paste 放入线程池，不阻塞主线程）"""
        if not self._running:
            self._poll_after_id = None
            return

        def on_paste_done(future):
            """线程池结果回调（通过 root.after 切回主线程处理）"""
            try:
                current_text = future.result()
                self.controller.root.after(0, lambda: self._handle_paste_result(current_text))
            except Exception as e:
                self.logger.debug(f"剪贴板读取错误: {e}")
            finally:
                # 调度下一次轮询
                if self._running:
                    try:
                        self._poll_after_id = self.controller.root.after(100, self._poll_once)
                    except Exception:
                        self._poll_after_id = None

        # 提交 pyperclip.paste() 到线程池
        try:
            future = self._executor.submit(pyperclip.paste)
            future.add_done_callback(on_paste_done)
        except Exception as e:
            self.logger.debug(f"提交剪贴板读取任务失败: {e}")
            if self._running:
                try:
                    self._poll_after_id = self.controller.root.after(100, self._poll_once)
                except Exception:
                    self._poll_after_id = None

    def _handle_paste_result(self, current_text: str):
        """主线程处理剪贴板读取结果"""
        if current_text and current_text != self._last_text and current_text != self._remote_text:
            self._last_text = current_text
            self._send_to_remote(current_text)

    def _send_to_remote(self, text: str):
        """发送剪贴板到远程"""
        if not self._sync_enabled:
            return
        if not self.controller._connected:
            return

        # 限制长度
        if len(text) > 1024 * 1024:  # 1MB
            self.logger.warning("剪贴板内容过大，跳过同步")
            return

        # 分段发送长文本
        max_chunk = 4096
        if len(text) > max_chunk:
            chunks = [text[i:i+max_chunk] for i in range(0, len(text), max_chunk)]
            for i, chunk in enumerate(chunks):
                self.controller.control_channel.send_json({
                    'type': 'clipboard',
                    'text': chunk,
                    'chunk': i,
                    'total': len(chunks)
                })
            self.logger.debug(f"剪贴板已分段发送: {len(chunks)}段")
        else:
            self.controller.control_channel.send_json({
                'type': 'clipboard',
                'text': text
            })
            self.logger.debug(f"剪贴板已发送: {text[:50]}...")

    def on_remote_clipboard(self, text: str, chunk: int = 0, total: int = 1):
        """收到远程剪贴板"""
        if not self._sync_enabled:
            return

        # 处理分段
        if total > 1:
            with self._lock:
                now = time.time()
                if chunk == 0:
                    self._pending_chunks = {}
                    self._chunk_start_time = now
                else:
                    # 检查是否超时
                    if now - self._chunk_start_time > self._chunk_timeout:
                        self._pending_chunks.clear()
                        self._chunk_start_time = now
                        self.logger.warning("剪贴板分片接收超时，已重置")
                        return

                self._pending_chunks[chunk] = text
                if len(self._pending_chunks) >= total:
                    # 所有分段已收到
                    full_text = ''.join(self._pending_chunks[i] for i in range(total))
                    self._remote_text = full_text
                    self._update_local_clipboard(full_text)
                    self._pending_chunks = {}
                    self._chunk_start_time = 0
            return

        self._remote_text = text
        self._update_local_clipboard(text)

    def _update_local_clipboard(self, text: str):
        """更新本地剪贴板（v4.0.1：pyperclip.copy 放入线程池）"""
        def on_copy_done(future):
            try:
                future.result()
                self._last_text = text
                self.logger.debug(f"远程剪贴板已接收: {text[:50]}...")
            except Exception as e:
                self.logger.error(f"更新本地剪贴板失败: {e}")

        try:
            self._executor.submit(pyperclip.copy, text).add_done_callback(on_copy_done)
        except Exception as e:
            self.logger.error(f"提交剪贴板写入任务失败: {e}")

    def enable_sync(self, enabled: bool):
        """启用/禁用剪贴板同步"""
        self._sync_enabled = enabled

    def stop(self):
        """停止监听（取消主线程定时器，关闭线程池）"""
        self._running = False
        if self._poll_after_id is not None:
            try:
                root = self.controller.root
                root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
