"""远端画面视图（阶段 6.6，v4 gui/screen_widget.py 迁移）。

架构：QGraphicsView + QGraphicsPixmapItem。场景坐标系 = **远端虚拟桌面
绝对坐标**（多显示器含负原点，帧自带 left/top），控件→远端坐标变换因此
是一条直线：``view.mapToScene(pos)`` 即远端绝对坐标。

帧投递：本件**不做解码**——client controller（阶段 9 接线）把解码后的
QImage 经 :meth:`present_image` 投来（qasync 同线程直调合法）。

交互：鼠标事件默认**只读回显**；``set_input_enabled(True)``（"启用控制"）
后按下/移动/滚轮/按键转成远端坐标 Signal 发回 main_window → controller。
v4 病灶不迁移：其坐标换算把"视图缩放因子"手算进除法且忽略负原点屏，
本实现用 QGraphicsView 场景变换矩阵，天然覆盖缩放/滚动/负原点。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QWidget

from rdc.ui.app import ensure_gui_thread

__all__ = ["ScreenView"]


class ScreenView(QGraphicsView):
    """远端屏幕画面 + 控制事件转发。

    Signals:
        mouseMoved(str, int, int): (action, 远端绝对 x, y)。action ∈
            ``move`` / ``press:left|right|middle`` / ``release:...`` /
            ``wheel:up|down``。
        keyEvent(int, bool): (Qt key, is_press)。
        fitModeChanged(bool): 适应窗口开关（main_window 记忆用）。

    线程红线：所有方法仅 GUI 线程调用（qasync 主线程协程直调合法）。
    """

    mouseMoved = Signal(str, int, int)
    keyEvent = Signal(int, bool)
    fitModeChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        ensure_gui_thread("ScreenView")
        super().__init__(parent)
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self._item = QGraphicsPixmapItem()
        scene.addItem(self._item)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # 左上对齐：视口原点 = 场景原点（远桌面语义；Qt 默认居中会把
        # 小帧居中浮显，坐标换算出现系统性偏移）
        self.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.setRenderHints(
            self.renderHints()  # 保默认
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)  # 悬停即回报移动
        self._input_enabled = False
        self._fit_mode = False
        self._remote_origin = QPointF(0, 0)  # 帧在虚拟桌面的 (left, top)
        self._remote_size: tuple[int, int] | None = None
        self._placeholder = True

    # -- 帧呈现 ---------------------------------------------------------------

    def present_image(self, image: QImage, *, left: int = 0, top: int = 0) -> None:
        """贴一帧解码后的画面。

        Args:
            image: 解码帧（QImage，格式不限，内部转 pixmap）。
            left/top: 该显示器在虚拟桌面中的原点（多显示器可为负）。
        """
        ensure_gui_thread("ScreenView.present_image")
        if image.isNull():
            return
        pix = QPixmap.fromImage(image)
        self._item.setPixmap(pix)
        self._item.setOffset(float(left), float(top))
        self._remote_origin = QPointF(float(left), float(top))
        self._remote_size = (image.width(), image.height())
        self._placeholder = False
        self.scene().setSceneRect(
            float(left), float(top), float(image.width()), float(image.height())
        )
        # Qt 默认居中场景；远桌面语义是左上对齐（多余视口空间是右下黑边）
        h_bar, v_bar = self.horizontalScrollBar(), self.verticalScrollBar()
        h_bar.setValue(h_bar.minimum())
        v_bar.setValue(v_bar.minimum())
        if self._fit_mode:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def clear_frame(self) -> None:
        """断开时清屏回到占位态。"""
        ensure_gui_thread("ScreenView.clear_frame")
        self._item.setPixmap(QPixmap())
        self._placeholder = True
        self._remote_size = None

    # -- 控制开关 ---------------------------------------------------------------

    def set_input_enabled(self, enabled: bool) -> None:
        """"启用控制"状态：True 后本视图输入的坐标/按键经 Signal 外发。"""
        ensure_gui_thread("ScreenView.set_input_enabled")
        self._input_enabled = enabled
        self.setCursor(
            Qt.CursorShape.ArrowCursor if enabled else Qt.CursorShape.OpenHandCursor
        )

    @property
    def input_enabled(self) -> bool:
        return self._input_enabled

    def set_fit_mode(self, fit: bool) -> None:
        """适应窗口 / 1:1 原始尺寸（全屏切换的底层开关）。"""
        ensure_gui_thread("ScreenView.set_fit_mode")
        self._fit_mode = fit
        if fit and self._remote_size is not None:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        else:
            self.resetTransform()
        self.fitModeChanged.emit(fit)

    @property
    def fit_mode(self) -> bool:
        return self._fit_mode

    # -- 坐标变换（公开以便单测直证） --------------------------------------------

    def remote_pos(self, viewport_pos: QPoint) -> QPointF | None:
        """视口像素坐标 → 远端虚拟桌面绝对坐标；无帧/越界返回 ``None``。"""
        scene_p = self.mapToScene(viewport_pos)
        if self._remote_size is None:
            return None
        x0, y0 = self._remote_origin.x(), self._remote_origin.y()
        w, h = self._remote_size
        if not (x0 <= scene_p.x() < x0 + w and y0 <= scene_p.y() < y0 + h):
            return None  # 越出远端屏（黑边区）——不发送输入，v4 同款保护
        return QPointF(scene_p)

    # -- Qt 事件覆写 -------------------------------------------------------------

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._emit("move", event.position().toPoint())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        btn = {Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right"}.get(
            event.button(), "middle"
        )
        self._emit(f"press:{btn}", event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        btn = {Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right"}.get(
            event.button(), "middle"
        )
        self._emit(f"release:{btn}", event.position().toPoint())
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if not self._input_enabled:
            super().wheelEvent(event)
            return
        direction = "up" if event.angleDelta().y() > 0 else "down"
        rp = self.remote_pos(event.position().toPoint())
        if rp is not None:
            self.mouseMoved.emit(f"wheel:{direction}", int(rp.x()), int(rp.y()))
        event.accept()  # 控制模式下吞滚轮（防误触视图缩放）

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._input_enabled:
            self.keyEvent.emit(event.key(), True)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._input_enabled:
            self.keyEvent.emit(event.key(), False)
            event.accept()
            return
        super().keyReleaseEvent(event)

    # -- 内部 ---------------------------------------------------------------------

    def _emit(self, action: str, viewport_pos: QPoint) -> None:
        if not self._input_enabled or self._placeholder:
            return
        rp = self.remote_pos(viewport_pos)
        if rp is not None:
            self.mouseMoved.emit(action, int(rp.x()), int(rp.y()))
