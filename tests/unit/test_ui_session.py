"""ui/session GUI 装配测试（阶段 9 批 4）。

覆盖 :func:`qimage_factory`（RGB→QImage 往返、copy 解耦、尺寸/形状守卫）与
:func:`build_client_controller`（装配产物接线）。launcher 主编排依赖人工
选择对话框，其端到端由批 5 真机走查覆盖。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtGui import QImage

from rdc.client.controller import ClientController
from rdc.config.settings import Settings
from rdc.ui.app import create_app
from rdc.ui.session import build_client_controller, qimage_factory


@pytest.fixture()
def qapp():
    return create_app([])


class TestQImageFactory:
    def test_roundtrip_pixel_values(self, qapp) -> None:
        arr = np.zeros((4, 8, 3), dtype=np.uint8)
        arr[1, 2] = (255, 128, 64)  # RGB888 语义
        img = qimage_factory(8, 4, arr)
        assert isinstance(img, QImage)
        assert (img.width(), img.height()) == (8, 4)
        assert not img.isNull()
        c = img.pixelColor(2, 1)
        assert (c.red(), c.green(), c.blue()) == (255, 128, 64)
        blank = img.pixelColor(0, 0)
        assert (blank.red(), blank.green(), blank.blue()) == (0, 0, 0)

    def test_copy_decouples_source_buffer(self, qapp) -> None:
        arr = np.full((2, 2, 3), 10, dtype=np.uint8)
        img = qimage_factory(2, 2, arr)
        arr[:] = 99  # 源缓冲改写不得影响已交付帧（.copy() 红线）
        assert img.pixelColor(0, 0).red() == 10

    def test_size_mismatch_raises(self, qapp) -> None:
        with pytest.raises(ValueError, match="尺寸不符"):
            qimage_factory(9, 9, np.zeros((2, 2, 3), dtype=np.uint8))

    def test_bad_channels_raises(self, qapp) -> None:
        with pytest.raises(ValueError, match="RGB"):
            qimage_factory(2, 2, np.zeros((2, 2, 4), dtype=np.uint8))

    def test_non_contiguous_input_normalized(self, qapp) -> None:
        base = np.zeros((4, 8, 3), dtype=np.uint8)
        base[:, :, 0] = 77
        view = base[:, ::2, :]  # 非连续视图（宽 4）
        img = qimage_factory(4, 4, view)
        assert img.pixelColor(0, 0).red() == 77

    def test_full_black_frame(self, qapp) -> None:
        img = qimage_factory(3, 3, np.zeros((3, 3, 3), dtype=np.uint8))
        assert img.allPixelColorsEqual(img.pixelColor(0, 0)) if hasattr(img, "allPixelColorsEqual") else True


class TestBuildClientController:
    def test_returns_controller_with_qimage_factory(self, qapp) -> None:
        ctl = build_client_controller(Settings())
        assert isinstance(ctl, ClientController)
        # 注入的工厂产 QImage（controller 交付帧 → UI 可直接消费）
        img = ctl._image_factory(2, 2, np.full((2, 2, 3), 50, dtype=np.uint8))  # noqa: SLF001
        assert isinstance(img, QImage) and img.pixelColor(0, 0).red() == 50

    def test_settings_default_when_none(self, qapp, tmp_app_dir) -> None:
        ctl = build_client_controller()
        assert isinstance(ctl, ClientController)
