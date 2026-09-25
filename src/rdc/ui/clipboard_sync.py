"""剪贴板同步（阶段 6.11 建立，阶段 7 批 E 扩展图片/文件）。

双驱动两级检测：
- **QClipboard.dataChanged 事件（主通道）**：任一类型变更时做全量快照
  （图片含 PNG 摘要，代价可接受——用户驱动、低频）并按类型分派信号；
- **QTimer 轮询（100ms 兜底，v4 ``root.after(100)`` 等价）**：只做**廉价快照**
  （文本摘要/文件路径摘要/图片尺寸键——绝不逐轮 PNG 编码），廉价键与上次
  内容键不一致时才升级全量确认。图片"同尺寸不同内容"的变更轮询无法区分，
  由 dataChanged 覆盖（Qt 进程内信号可靠；轮询仅防 Windows 罕见漏发）。

载荷类型（批 E，Windows 优先；QClipboard 跨平台语义一致，offscreen 探针实测）：
- **text**：纯文本（阶段 6 基线，:attr:`localChanged` 信号名与语义原样保留）；
- **image**：位图（:attr:`localImageChanged` 携带解码后的 QImage）；
- **files**：本地文件 URL 列表（:attr:`localFilesChanged`；Windows 侧即
  CF_HDROP——探针实测 ``setUrls`` 往返可用，且复制文件时 Qt 的
  ``mimeData().hasText()`` 同时为真，故类型判定优先级 ``urls > image > text``）。

防回环：``apply_remote_*`` 先置抑制标记再写入，并以**读回**的快照为基线
（Qt 可能重编码图片字节，读回计算摘要才不会自触发）。

大内容红线：图片字节与文件清单只在本机剪贴板 ↔ 内存对象间流转；协议侧
``ClipboardEvent`` 的分块传输装配归阶段 9，本件不做网络 I/O。

v4 病灶不迁移：其 ``_handle_paste_result`` 用 future 回调 + root.after 双层
调度；v5 同线程结构下全部退化为同步代码。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from rdc.ui.app import ensure_gui_thread

__all__ = [
    "ClipboardFilesPayload",
    "ClipboardImagePayload",
    "ClipboardSync",
    "ClipboardTextPayload",
]


@dataclass(frozen=True)
class ClipboardTextPayload:
    """文本载荷。"""

    text: str


@dataclass(frozen=True)
class ClipboardImagePayload:
    """图片载荷（解码后的 QImage 原样，跨进程编码归阶段 9 装配）。"""

    image: QImage


@dataclass(frozen=True)
class ClipboardFilesPayload:
    """文件载荷：绝对路径元组（本机文件存在性由调用方保证）。"""

    paths: tuple[str, ...]


#: 快照三元组：(载荷, 廉价键, 全量键)。廉价键轮询可算；全量键含图片摘要。
_Snapshot = tuple[
    ClipboardTextPayload | ClipboardImagePayload | ClipboardFilesPayload | None,
    str,
    str,
]


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _digest_image(image: QImage) -> str:
    """图片内容摘要（PNG 编码哈希；仅全量路径调用，轮询不触碰）。"""
    if image.isNull():
        return "null"
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # PySide6 6.11 存根/运行时错位（探针实测）：save 的 format 存根标 bytes
    # 运行时却只收 str；bytes(QByteArray) 反之——存根不收、运行时支持 buffer
    # 协议。两处以 ignore 固定，运行时行为已验证。
    image.save(buffer, "PNG")  # type: ignore[call-overload]
    return hashlib.sha256(bytes(buffer.data())).hexdigest()  # type: ignore[call-overload]


class ClipboardSync(QObject):
    """双向剪贴板同步器（文本 + 图片 + 文件）。

    Signals:
        localChanged(str): 本地剪贴板出现新文本（去抖后；阶段 6 基线原样）。
        localImageChanged(QImage): 本地出现新图片（批 E 扩展）。
        localFilesChanged(list): 本地出现新文件清单（URL 字符串列表；批 E）。

    Args:
        interval_ms: 轮询周期（v4 同值 100ms）。
        parent: QObject 父件。
    """

    localChanged = Signal(str)
    localImageChanged = Signal(QImage)
    localFilesChanged = Signal(list)

    def __init__(self, *, interval_ms: int = 100, parent: QObject | None = None) -> None:
        ensure_gui_thread("ClipboardSync")
        super().__init__(parent)
        app = QApplication.instance()
        if not isinstance(app, QApplication):  # pragma: no cover - 构造前置条件
            raise RuntimeError("ClipboardSync 需要已创建 QApplication")
        self._clip = app.clipboard()
        self._enabled = True
        self._suppress = False  # apply_remote_* 回写抑制标记
        _, self._last_cheap, self._last_full = self._snapshot(full_image=True)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll_once)
        self._clip.dataChanged.connect(self._on_data_changed)

    # -- 生命周期 -----------------------------------------------------------------

    def start(self) -> None:
        """开始轮询（幂等）。"""
        ensure_gui_thread("ClipboardSync.start")
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """停止轮询（幂等；禁用同步亦经此）。"""
        ensure_gui_thread("ClipboardSync.stop")
        self._timer.stop()

    def set_enabled(self, enabled: bool) -> None:
        """开关同步（``enable_clipboard`` 设置项映射；关闭即停轮询）。"""
        ensure_gui_thread("ClipboardSync.set_enabled")
        self._enabled = enabled
        if enabled:
            self.start()
        else:
            self.stop()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- 远端 → 本地 ----------------------------------------------------------------

    def apply_remote(self, text: str) -> None:
        """（基线兼容）写入远端文本并更新基线（防回环）。"""
        ensure_gui_thread("ClipboardSync.apply_remote")
        self._write(lambda: self._clip.setText(text))

    def apply_remote_image(self, image: QImage) -> None:
        """写入远端图片并更新基线（防回环）。"""
        ensure_gui_thread("ClipboardSync.apply_remote_image")
        self._write(lambda: self._clip.setImage(image))

    def apply_remote_files(self, paths: list[str]) -> None:
        """写入远端文件 URL 清单并更新基线（防回环）。

        ⚠️ 跨机语义：写入的是**本机**路径清单（粘贴目标由文件传输通道兑现，
        归阶段 8/9）；本件不做网络 I/O。
        """
        ensure_gui_thread("ClipboardSync.apply_remote_files")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        self._write(lambda: self._clip.setMimeData(mime))

    # -- 检测 ---------------------------------------------------------------------

    def _poll_once(self) -> None:
        """轮询兜底：廉价快照，命中差异才升级全量确认。"""
        ensure_gui_thread("ClipboardSync._poll_once")
        if self._suppress or not self._enabled:
            return
        _, cheap, _ = self._snapshot(full_image=False)
        if cheap == self._last_cheap:
            return
        self._evaluate(full_image=False)

    def _on_data_changed(self) -> None:
        """dataChanged 主通道：全量快照分派（urls > image > text）。"""
        if self._suppress or not self._enabled:
            return
        self._evaluate(full_image=True)

    def _evaluate(self, *, full_image: bool) -> None:
        """按快照差异分派信号；图片轮询路径先全量确认再发。"""
        payload, cheap, full = self._snapshot(full_image=full_image)
        if full_image:
            if full == self._last_full:
                self._last_cheap = cheap  # 内容未变（如仅格式抖动），静默对齐
                return
        else:
            # 轮询廉价路径：图片"同尺寸不同内容"区分不了 → 升全量核对
            if isinstance(payload, ClipboardImagePayload):
                _, _, full = self._snapshot(full_image=True)
                if full == self._last_full:
                    return
            elif cheap == self._last_cheap:  # pragma: no cover - 前置已拦
                return
        self._last_cheap = cheap
        self._last_full = full
        match payload:
            case ClipboardTextPayload(text):
                self.localChanged.emit(text)
            case ClipboardImagePayload(image):
                self.localImageChanged.emit(image)
            case ClipboardFilesPayload(paths):
                self.localFilesChanged.emit(list(paths))
            case None:  # 剪贴板被清空：基线更新，不发信号
                pass

    # -- 内部 ---------------------------------------------------------------------

    def _write(self, fn: object) -> None:
        """抑制标记下写入，并以读回快照为新基线（防回环+格式归一）。"""
        self._suppress = True
        try:
            fn()  # type: ignore[operator]
            _, self._last_cheap, self._last_full = self._snapshot(full_image=True)
        finally:
            self._suppress = False

    def _snapshot(self, *, full_image: bool) -> _Snapshot:
        """当前剪贴板快照。

        Args:
            full_image: True 时图片键含 PNG 摘要（精确但贵）；False 时用
                尺寸键（轮询专用）。非图片类型两种模式同键。

        offscreen/无桌面会话下 ``mimeData()`` 可能返回 ``None``（空剪贴板），
        视为空——阶段 6 的 ``clip.text()`` 同样场景返回 ``""``，语义对齐。
        """
        mime = self._clip.mimeData()
        if mime is None:
            return None, "empty", "empty"
        if mime.hasUrls():
            paths = tuple(u.toLocalFile() for u in mime.urls() if u.isLocalFile())
            if paths:
                digest = _digest_text("|".join(paths))
                key = f"files:{digest}"
                return ClipboardFilesPayload(paths), key, key
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                cheap = f"image:{image.width()}x{image.height()}x{image.sizeInBytes()}"
                full_key = (
                    f"image:{_digest_image(image)}" if full_image else cheap
                )
                return ClipboardImagePayload(image), cheap, full_key
        if mime.hasText():
            text = mime.text()
            key = f"text:{_digest_text(text)}"
            return ClipboardTextPayload(text), key, key
        return None, "empty", "empty"
