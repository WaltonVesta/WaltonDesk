"""生成 WaltonDesk 多尺寸图标（阶段 10）。

输入：项目根 ``icon.png``（设计稿，实测 1183×1220 RGBA，四角透明）。
输出（两处，理由见 :mod:`rdc.ui.icons`）：

- 项目根 ``icon.ico`` —— PyInstaller ``icon=`` 与 Inno Setup ``SetupIconFile``
  需要构建期磁盘路径；
- ``src/rdc/ui/theme/icon.ico`` —— 运行期唯一加载点（已随 ``theme/`` 进 spec
  的 datas，源码/frozen 两形态同路径）。

用法::

    uv run python scripts/gen_icon.py

幂等：每次全量重建两个文件，可重复执行。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "icon.png"
OUTS = (ROOT / "icon.ico", ROOT / "src" / "rdc" / "ui" / "theme" / "icon.ico")

#: ICO 标准尺寸集：覆盖 16px 托盘到 256px 高清桌面图标 / 安装程序图标。
#: 少一个尺寸，Windows 就会挑最近尺寸放大拉伸（16px 上尤其明显地糊）。
SIZES: tuple[int, ...] = (16, 20, 24, 32, 48, 64, 96, 128, 256)


def build_image() -> Image.Image:
    """读设计稿并补成正方形（LANCZOS 保比例，避免 logo 被压扁）。

    设计稿 1183×1220 不是正方形；直接 resize 到 256×256 会让 W 变形。
    先在透明画布上居中成正方形，再缩放。
    """
    src = Image.open(SOURCE).convert("RGBA")
    w, h = src.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(src, ((side - w) // 2, (side - h) // 2), src)
    return canvas


def write_ico(image: Image.Image, target: Path) -> None:
    """按标准尺寸集写出单个 .ico。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"[ok] {target.relative_to(ROOT)}  {target.stat().st_size} bytes")


def main() -> int:
    """生成图标；设计稿缺失时返回非零退出码（构建脚本可据此失败）。"""
    if not SOURCE.is_file():
        print(f"[FAIL] 找不到设计稿: {SOURCE}", file=sys.stderr)
        return 1
    image = build_image()
    for out in OUTS:
        write_ico(image, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
