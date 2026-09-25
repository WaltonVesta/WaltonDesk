"""设置对话框（阶段 6.8，v4 gui/settings_dialog.py 迁移）。

QTabWidget 三页签（通用/网络/安全），读写 v5 :class:`~rdc.config.settings.Settings`
（阶段 2 的 Pydantic 模型，v4 的 Config 单例不再触碰）。

v4 差异：
- v4 保存走 Config 单例写文件；v5 ``apply(current) -> Settings`` 返回新值对象，
  持久化（save_settings + 校验 apply_validators）由调用方（main_window）负责，
  本件不写文件不碰 secrets——对话框纯编辑视图；
- v4 的"记住密码(不可用)"禁用态：v5 remember_password 是 Settings 真实字段
  （密码本体经 secrets 存储由会话层处理），此处为可用复选框。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.config.settings import Settings
from rdc.ui.app import ensure_gui_thread

__all__ = ["SettingsDialog"]


class SettingsDialog(QDialog):
    """三页签设置编辑对话框。

    Args:
        settings: 初始值（通常 current_settings()）。
        parent: 宿主窗口。
    """

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        ensure_gui_thread("SettingsDialog")
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(480, 400)
        self._initial = settings

        tabs = QTabWidget()
        tabs.addTab(self._build_general(), "通用")
        tabs.addTab(self._build_network(), "网络")
        tabs.addTab(self._build_security(), "安全")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

        self._load_from(settings)

    # -- 页签构建 ---------------------------------------------------------------

    def _build_general(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(1, 100)
        q_box = QHBoxLayout()
        self.quality_value = QLabel("--")
        self.quality_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        q_box.addWidget(self.quality_slider, 1)
        q_box.addWidget(self.quality_value)
        self.quality_slider.valueChanged.connect(
            lambda v: self.quality_value.setText(f"{v}%")
        )
        form.addRow("默认画质:", self._wrap(q_box))
        self.max_fps = self._spin(1, 120)
        self.min_fps = self._spin(1, 60)
        self.fps_range = QLabel("画质范围: 30 - 95")
        self.use_diff = QCheckBox("启用差分检测 (减少带宽)")
        self.enable_compression = QCheckBox("启用数据压缩")
        form.addRow("最大帧率:", self.max_fps)
        form.addRow("最小帧率:", self.min_fps)
        form.addRow("", self.fps_range)
        form.addRow("", self.use_diff)
        form.addRow("", self.enable_compression)
        return w

    def _build_network(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.port = self._spin(1, 65535)
        self.control_port = self._spin(1, 65535)
        self.max_connections = self._spin(1, 128)
        self.bandwidth_limit = self._spin(0, 10_000_000, suffix=" KB/s")
        self.auto_reconnect = QCheckBox("启用自动重连")
        self.reconnect_delay = self._spin(1, 300, suffix=" 秒")
        self.max_reconnect = self._spin(1, 100)
        form.addRow("服务端口:", self.port)
        form.addRow("控制端口:", self.control_port)
        form.addRow("最大连接数:", self.max_connections)
        form.addRow("带宽限制 (0=无限制):", self.bandwidth_limit)
        form.addRow("", self.auto_reconnect)
        form.addRow("重连延迟:", self.reconnect_delay)
        form.addRow("最大重试次数:", self.max_reconnect)
        return w

    def _build_security(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.enable_auth = QCheckBox("启用认证 (需要设置密码)")
        self.remember_password = QCheckBox("记住密码")
        form.addRow("", self.enable_auth)
        form.addRow("", self.remember_password)
        tls_note = QLabel("🔐 TLS 1.2+ 加密已强制开启（自签名证书自动生成）")
        tls_note.setStyleSheet("color: #9aa0a6;")
        form.addRow(tls_note)
        self.enable_clipboard = QCheckBox("启用剪贴板同步")
        form.addRow("", self.enable_clipboard)
        return w

    # -- 值绑定（单一入口，双向） --------------------------------------------------

    def _load_from(self, s: Settings) -> None:
        ensure_gui_thread("SettingsDialog._load_from")
        self.quality_slider.setValue(s.jpeg_quality)
        self.max_fps.setValue(s.max_fps)
        self.min_fps.setValue(s.min_fps)
        self.use_diff.setChecked(s.use_diff)
        self.enable_compression.setChecked(s.enable_compression)
        self.port.setValue(s.port)
        self.control_port.setValue(s.control_port)
        self.max_connections.setValue(s.max_connections)
        self.bandwidth_limit.setValue(s.bandwidth_limit)
        self.auto_reconnect.setChecked(s.auto_reconnect)
        self.reconnect_delay.setValue(int(s.reconnect_delay))
        self.max_reconnect.setValue(s.max_reconnect_attempts)
        self.enable_auth.setChecked(s.enable_auth)
        self.remember_password.setChecked(s.remember_password)
        self.enable_clipboard.setChecked(s.enable_clipboard)

    def apply(self, base: Settings | None = None) -> Settings:
        """把当前表单值合入 Settings，返回**新对象**（不落盘、不校验）。

        Args:
            base: 合并基底；``None`` 用打开对话框时的初始快照（保留表单外字段）。

        Returns:
            更新后的 Settings 值对象。
        """
        ensure_gui_thread("SettingsDialog.apply")
        s = base or self._initial
        return s.model_copy(
            update={
                "jpeg_quality": self.quality_slider.value(),
                "max_fps": self.max_fps.value(),
                "min_fps": self.min_fps.value(),
                "use_diff": self.use_diff.isChecked(),
                "enable_compression": self.enable_compression.isChecked(),
                "port": self.port.value(),
                "control_port": self.control_port.value(),
                "max_connections": self.max_connections.value(),
                "bandwidth_limit": self.bandwidth_limit.value(),
                "auto_reconnect": self.auto_reconnect.isChecked(),
                "reconnect_delay": float(self.reconnect_delay.value()),
                "max_reconnect_attempts": self.max_reconnect.value(),
                "enable_auth": self.enable_auth.isChecked(),
                "remember_password": self.remember_password.isChecked(),
                "enable_clipboard": self.enable_clipboard.isChecked(),
            }
        )

    # -- 小工具 -------------------------------------------------------------------

    def _spin(self, lo: int, hi: int, *, suffix: str = "") -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        if suffix:
            sp.setSuffix(suffix)
        return sp

    @staticmethod
    def _wrap(layout: QHBoxLayout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder
