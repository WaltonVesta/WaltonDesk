"""远端画面视图（阶段 6.6，v4 gui/screen_widget.py 迁移；阶段 7 批 E 扩展 IME）。

架构：QGraphicsView + QGraphicsPixmapItem。场景坐标系 = **远端虚拟桌面
绝对坐标**（多显示器含负原点，帧自带 left/top），控件→远端坐标变换因此
是一条直线：``view.mapToScene(pos)`` 即远端绝对坐标。

帧投递：本件**不做解码**——client controller（阶段 9 接线）把解码后的
QImage 经 :meth:`present_image` 投来（qasync 同线程直调合法）。

交互：鼠标事件默认**只读回显**；``set_input_enabled(True)``（"启用控制"）
后按下/移动/滚轮/按键转成远端坐标 Signal 发回 main_window → controller。
v4 病灶不迁移：其坐标换算把"视图缩放因子"手算进除法且忽略负原点屏，
本实现用 QGraphicsView 场景变换矩阵，天然覆盖缩放/滚动/负原点。

键盘双通道（批 E，阶段 7 决策点 3"发送侧不破坏 IME、接收侧不模拟 IME"）：
- **物理按键一律** :attr:`keyEvent`（Qt 键码，与阶段 6 基线完全一致）——
  装配层经 :func:`rdc.input.keys.qt_key_to_name` 映射为规范键名注入，
  远端键盘布局与远端 IME 行为由被控端自然决定；
- **IME 候选窗提交串**（控制端本地输入法"已上屏"文本）→ :attr:`textCommitted`
  ——装配层映射到 ``InputBackend.type_text``（KEYEVENTF_UNICODE），保证
  "控制端上屏什么，远端出什么"，不受远端布局/IME 二次劫持。
候选窗组合态（preedit）由控制端本地 IME 自理，**永不进协议**。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QWidget

from rdc.ui.app import ensure_gui_thread

__all__ = ["ScreenView"]


class ScreenView(QGraphicsView):
    """远端屏幕画面 + 控制事件转发。

    Signals:
        mouseMoved(str, int, int): (action, 远端绝对 x, y)。action ∈
            ``move`` / ``press:left|right|middle`` / ``release:...`` /
            ``wheel:up|down``。
        keyEvent(int, bool): (Qt key, is_press)。全部物理按键（阶段 6 基线）。
        textCommitted(str): IME 候选窗提交的已上屏文本（批 E 新增通道）。
        fitModeChanged(bool): 适应窗口开关（main_window 记忆用）。

    缩放策略（走查第五批修正）：默认 **适应窗口**——等比缩放撑满视口某
    一维度、居中、剩余黑边（VNC/TeamViewer 同语义）；``set_fit_mode(False)``
    回 1:1 原始尺寸（左上对齐 + 滚动条）。坐标换算走 ``mapToScene`` 变换
    矩阵，两种模式都天然正确。

    线程红线：所有方法仅 GUI 线程调用（qasync 主线程协程直调合法）。
    """

    mouseMoved = Signal(str, int, int)
    keyEvent = Signal(int, bool)
    textCommitted = Signal(str)
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
        # 默认 fit：场景居中于视口（黑边对称）；1:1 模式切回左上对齐
        # （远桌面语义，多余视口空间在右下）
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 黑边纯黑（主题 Base 色是深灰，等比缩放留边必须显式压黑）
        self.setBackgroundBrush(QBrush(QColor(0, 0, 0)))
        # 等比放大/缩小时平滑插值（远桌面文字不锯齿）
        self.setRenderHints(
            self.renderHints() | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)  # 悬停即回报移动
        self._input_enabled = False
        self._fit_mode = True
        self._remote_origin = QPointF(0, 0)  # 帧在虚拟桌面的 (left, top)
        self._remote_size: tuple[int, int] | None = None
        self._placeholder = True
        #: IME 提交事件落在 viewport（焦点代理），输入方法事件无冒泡，必须过滤。
        self.viewport().installEventFilter(self)

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
        size_changed = self._remote_size != (image.width(), image.height())
        pix = QPixmap.fromImage(image)
        self._item.setPixmap(pix)
        self._item.setOffset(float(left), float(top))
        self._remote_origin = QPointF(float(left), float(top))
        self._remote_size = (image.width(), image.height())
        self._placeholder = False
        self.scene().setSceneRect(
            float(left), float(top), float(image.width()), float(image.height())
        )
        if self._fit_mode:
            # 分辨率变化（虚拟机窗口调整/切屏）才重算等比缩放；
            # 每帧 fitInView 会让变换矩阵反复重设、画面抖动
            if size_changed:
                self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        else:
            # 1:1 远桌面语义：左上对齐，多余视口空间是右下黑边
            h_bar, v_bar = self.horizontalScrollBar(), self.verticalScrollBar()
            h_bar.setValue(h_bar.minimum())
            v_bar.setValue(v_bar.minimum())

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
        """适应窗口（等比居中黑边）/ 1:1 原始尺寸（左上+滚动条）。"""
        ensure_gui_thread("ScreenView.set_fit_mode")
        self._fit_mode = fit
        if fit and self._remote_size is not None:
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        elif fit:
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.resetTransform()
        else:
            self.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            self.resetTransform()
        self.fitModeChanged.emit(fit)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt 覆写签名
        """视口尺寸变化（拖窗/全屏进出）：fit 模式重算等比缩放。"""
        super().resizeEvent(event)
        if self._fit_mode and self._remote_size is not None:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

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
        if self._input_enabled:
            # 走查第六批 Bug2：点击画面即抓取键盘焦点。此前焦点常留在
            # 工具栏/无焦点，Tab 被 Qt 焦点导航消费、Win 触发主控端开始
            # 菜单（"泄漏"）。焦点在本视图后，keyPressEvent 对全部按键
            # accept()，事件不再外溢。
            self.setFocus()
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

    def focusNextPrevChild(self, next: bool) -> bool:  # noqa: N802 - Qt 覆写签名
        """走查第十批：控制开启时把 Tab/Backtab 留给远端，不做本地焦点跳转。

        Qt 在 ``QWidget::event()`` 里把无修饰 Tab/Backtab 直接转成本方法，
        **发生在 keyPressEvent 之前**——控件级 ``event.accept()`` 拦不住
        （第六批误判：以为 accept 后事件不外溢，实测焦点仍跳、零转发）。
        本覆写是病灶正解：返回 False 后 Qt 把按键继续按普通按键投递给
        keyPressEvent → keyEvent 转发远端。判定用 ``_input_enabled`` 实时
        读，与 :meth:`set_input_enabled` 同步，无需安装/卸载。
        """
        if self._input_enabled:
            return False
        return super().focusNextPrevChild(next)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._input_enabled:
            self.keyEvent.emit(event.key(), False)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:  # noqa: N802
        """IME 提交（commit 非空即"已上屏"）→ 文本通道；组合态不外发。

        控制端本地候选窗由 Qt/系统 IME 自理（preedit 段丢弃），仅 commit
        串转发远端——阶段 7 决策点 3 的"接收侧不模拟 IME"口径。生产环境
        输入方法事件落在 viewport（焦点代理），由 :meth:`eventFilter`
        截获；本覆写覆盖直接投递到 view 的路径（单测/内嵌场景）。
        """
        if self._input_enabled and event.commitString():
            self.textCommitted.emit(event.commitString())
            event.accept()
            return
        super().inputMethodEvent(event)

    def eventFilter(self, obj: object, event: object) -> bool:  # noqa: N802
        """viewport 上截获 IME 提交（输入方法事件无冒泡，必须过滤器）。"""
        if (
            obj is self.viewport()
            and isinstance(event, QInputMethodEvent)
            and self._input_enabled
            and event.commitString()
        ):
            self.textCommitted.emit(event.commitString())
            return True
        return super().eventFilter(obj, event)  # type: ignore[arg-type]

    # -- 内部 ---------------------------------------------------------------------

    def _emit(self, action: str, viewport_pos: QPoint) -> None:
        if not self._input_enabled or self._placeholder:
            return
        rp = self.remote_pos(viewport_pos)
        if rp is not None:
            self.mouseMoved.emit(action, int(rp.x()), int(rp.y()))
