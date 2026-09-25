"""ClipboardSync 图片/文件扩展测试（阶段 7 批 E）。

offscreen 平台下 QClipboard 真实往返（探针实测口径：图片 setImage/imageData
可往返、setUrls 往返但 toLocalFile 因 Qt 内部规范化与传入路径字符串可能大小写
/分隔符差异，故比较用规范化的同一性而非字面相等）。基线文本行为（阶段 6）
已在 test_ui_dialogs 锁定，本文件只补批 E 新增面，并复跑关键基线防回归。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from rdc.ui.clipboard_sync import ClipboardSync


def _img(w: int = 8, h: int = 8, rgb: tuple[int, int, int] = (1, 2, 3)) -> QImage:
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(QColor(*rgb, 255))
    return image


@pytest.fixture
def clip(qapp) -> ClipboardSync:  # noqa: ANN001 - pytest-qt 夹具
    # QClipboard 是进程级全局：先清空，防上一用例内容污染基线
    # （ClipboardSync.__init__ 以当前内容为基线，语义正确，问题在测试隔离）。
    QApplication.clipboard().clear()
    cs = ClipboardSync(interval_ms=20)
    cs.start()
    yield cs
    cs.stop()
    cs.deleteLater()


def _digest_image(image: QImage) -> bytes:
    buffer = image.constBits()
    buffer.setsize(image.sizeInBytes())
    return bytes(buffer[:64])


class TestBaselineTextRegression:
    """阶段 6 文本基线不得被批 E 重写破坏（复跑核心三条）。"""

    def test_text_change_emits_once(self, clip: ClipboardSync, qtbot) -> None:
        seen: list[str] = []
        clip.localChanged.connect(seen.append)
        QApplication.clipboard().setText("hello-v7")
        qtbot.waitUntil(lambda: seen == ["hello-v7"], timeout=2000)

    def test_apply_remote_text_no_echo(self, clip: ClipboardSync, qtbot) -> None:
        seen: list[str] = []
        clip.localChanged.connect(seen.append)
        clip.apply_remote("from-remote")
        assert QApplication.clipboard().text() == "from-remote"
        qtbot.wait(150)
        assert seen == []

    def test_disable_stops_polling(self, clip: ClipboardSync, qtbot) -> None:
        seen: list[str] = []
        clip.localChanged.connect(seen.append)
        clip.set_enabled(False)
        QApplication.clipboard().setText("ignored")
        qtbot.wait(150)
        assert seen == []


class TestImagePayload:
    def test_local_image_change_emits_decoded_image(self, clip: ClipboardSync, qtbot) -> None:
        seen: list[QImage] = []
        clip.localImageChanged.connect(seen.append)
        clip.localChanged.connect(lambda _t: None)  # 干扰通道
        QApplication.clipboard().setImage(_img(16, 9, (10, 20, 30)))
        qtbot.waitUntil(lambda: len(seen) == 1, timeout=2000)
        assert seen[0].width() == 16 and seen[0].height() == 9
        assert seen[0].pixelColor(0, 0) == QColor(10, 20, 30, 255)

    def test_apply_remote_image_no_echo(self, clip: ClipboardSync, qtbot) -> None:
        images: list[QImage] = []
        clip.localImageChanged.connect(images.append)
        clip.apply_remote_image(_img(4, 4, (5, 5, 5)))
        assert QApplication.clipboard().image().width() == 4
        qtbot.wait(150)
        assert images == []

    def test_same_image_reapplied_not_reemitted(self, clip: ClipboardSync, qtbot) -> None:
        """同图重复 set（字节/内容一致）不得重复外发（去抖核心）。"""
        images: list[QImage] = []
        clip.localImageChanged.connect(images.append)
        QApplication.clipboard().setImage(_img(4, 4, (5, 5, 5)))
        qtbot.waitUntil(lambda: len(images) == 1, timeout=2000)
        QApplication.clipboard().setImage(_img(4, 4, (5, 5, 5)))
        qtbot.wait(200)
        assert len(images) == 1


class TestFilesPayload:
    def test_local_urls_emit_paths(self, clip: ClipboardSync, qtbot, tmp_path) -> None:
        import os.path

        p1 = tmp_path / "a.txt"
        p2 = tmp_path / "b.txt"
        p1.write_text("1")
        p2.write_text("2")
        seen: list[list[str]] = []
        clip.localFilesChanged.connect(seen.append)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p1)), QUrl.fromLocalFile(str(p2))])
        QApplication.clipboard().setMimeData(mime)
        qtbot.waitUntil(lambda: len(seen) == 1, timeout=2000)
        assert len(seen[0]) == 2
        # Qt toLocalFile() 统一返回正斜杠（探针实测），断言按规范化路径比较
        norm = {os.path.normcase(os.path.normpath(p)) for p in seen[0]}
        assert norm == {
            os.path.normcase(os.path.normpath(str(p1))),
            os.path.normcase(os.path.normpath(str(p2))),
        }

    def test_apply_remote_files_no_echo(self, clip: ClipboardSync, qtbot, tmp_path) -> None:
        p = tmp_path / "c.bin"
        p.write_bytes(b"\x00\x01")
        seen: list[list[str]] = []
        clip.localFilesChanged.connect(seen.append)
        clip.localChanged.connect(lambda _t: None)
        clip.apply_remote_files([str(p)])
        urls = QApplication.clipboard().mimeData().urls()
        assert len(urls) == 1
        qtbot.wait(150)
        assert seen == []

    def test_files_type_wins_over_text(self, clip: ClipboardSync, qtbot, tmp_path) -> None:
        """Windows 复制文件时 Qt 同时带路径文本——判定优先级 urls > text。"""
        p = tmp_path / "d.txt"
        p.write_text("x")
        files: list[list[str]] = []
        texts: list[str] = []
        clip.localFilesChanged.connect(files.append)
        clip.localChanged.connect(texts.append)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p))])
        mime.setText(str(p))  # 同时带文本（Qt 对 CF_HDROP 的常见表现）
        QApplication.clipboard().setMimeData(mime)
        qtbot.waitUntil(lambda: len(files) == 1, timeout=2000)
        qtbot.wait(150)
        assert texts == []  # 不得被误判为文本变更


class TestClearEdge:
    def test_clear_clipboard_no_signal_but_baseline_updates(
        self, clip: ClipboardSync, qtbot
    ) -> None:
        clip.apply_remote("seed")
        QApplication.clipboard().clear()
        seen_text: list[str] = []
        clip.localChanged.connect(seen_text.append)
        qtbot.wait(150)
        assert seen_text == []
        QApplication.clipboard().setText("after-clear")
        qtbot.waitUntil(lambda: seen_text == ["after-clear"], timeout=2000)


class TestWorkerThreadGuard:
    def test_apply_remote_image_rejects_worker(self, clip: ClipboardSync) -> None:
        import threading

        errors: list[BaseException] = []

        def run() -> None:
            try:
                clip.apply_remote_image(_img())
            except BaseException as exc:  # noqa: BLE001 - 收集跨线程断言异常
                errors.append(exc)

        t = threading.Thread(target=run)
        t.start()
        t.join(2)
        assert errors and isinstance(errors[0], RuntimeError)
