# -*- mode: python ; coding: utf-8 -*-
"""WaltonDesk v5.0.0 — PyInstaller 打包配置（阶段 10.1，目录模式 -D）。

构建::

    uv run --group package pyinstaller waltondesk.spec --noconfirm

产物::

    dist/WaltonDesk/WaltonDesk.exe        # 主程序（约 11 MB）
    dist/WaltonDesk/_internal/...         # 依赖与数据文件

设计要点（每条都是实测踩过的坑，改前先读理由）：

1. **console=True**（不是 windowed）。被控端 headless 形态要在终端打印横幅与
   初始密码、要 Ctrl+C；``--version``/``--help`` 由 argparse 内部写
   ``sys.stdout``，windowed 下该流为 ``None`` → 直接 AttributeError 崩（第十二批
   打包守卫修的就是这类）。GUI / 托盘形态改由
   :func:`rdc.desktop_entry.frozen_entry` 在启动早期 ``FreeConsole()`` 释放黑框
   —— 与 ``rdc.console_safety`` 的"stdin None = 无控制台 = 必须起 GUI"三态判定
   天然自洽。**勿改成 console=False。**

2. **datas 必须显式带两个目录**，漏一个 exe 就闪退：
   - ``rdc/protocol/pb``：Protobuf 生成物（本机由 ``scripts/gen_proto.py`` 生成、
     被 .gitignore 忽略，运行时必需）；
   - ``rdc/ui/theme``：``dark.qss`` 经 ``importlib.resources`` 读取（漏带 →
     ``create_app`` 抛 FileNotFoundError 启动即闪退）；同目录还有运行期加载的
     ``icon.ico``（见 :mod:`rdc.ui.icons`）。

3. **PySide6 只收集三个模块，绝不 collect_all("PySide6")**。实测首轮用
   collect_all 全量收集，产物 **899 MB**，其中光 ``Qt6WebEngineCore.dll`` 就
   194 MB、其 devtools 资源 .pak 再 83 MB、Designer/Pdf/Qml/Quick 一堆全用不上。
   源码实际 import 面（全树 grep 实测）只有 ``QtCore/QtGui/QtWidgets``——按这三
   个收集子模块，Qt6 DLL 与平台插件交给 PyInstaller 官方 PySide6 hook 依 import
   关系自动收（首轮实测 ``platforms/qwindows.dll`` 正常入包）。
   ⚠️ 若将来引入 QtNetwork/QtSvg 等，必须同步加进 ``PYSIDE_MODULES``，否则
   frozen 下 ``ImportError``（源码里能跑、exe 打不开，是这类问题最难查的形态）。

4. **av / shiboken6 用 collect_all**：两者包目录内有大量非 import 可达的二进制
   —— av 带整套 FFmpeg DLL（``av.libs/avcodec-62-*.dll`` 18 MB、libx265 12 MB，
   H.264 编码必需），shiboken6 带 ``shiboken6.abi3.dll``（``ui/main_window.py``
   直接 ``import shiboken6`` 做对象存活判定，走查第六批 Bug5）。这两处省不得。

5. **cv2 / numpy 不 collect_all**：官方 hook 已覆盖（opencv-python-headless 的
   cv2.pyd 82 MB + videoio ffmpeg 29 MB 即由此而来，属依赖固有体积，非配置冗余；
   源码仅用 absdiff/cvtColor/imencode/resize/connectedComponentsWithStats，
   换库收益有限，留 v5.1 评估）。

6. **upx=False / strip=False**：本机无 UPX（且 UPX 压过的 PySide6/av DLL 易触发
   杀软误报）；Windows 无 strip 工具。

7. **excludes 挡掉测试与开发工具**：pytest/pytestqt/hypothesis 一旦进包，体积
   膨胀且把 Qt Test 模块拖进来；tkinter 是 v4 遗留依赖，v5 源码零引用（全树 grep
   实测），挡掉省 ~5 MB。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

# 源码实际 import 的 Qt 模块（全树 grep 实测：QtCore/QtGui/QtWidgets）。
# 新增 Qt 模块时必须同步加这里，否则 frozen 下 ImportError。
PYSIDE_MODULES = ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets")

datas: list[tuple[str, str]] = [
    ("src/rdc/protocol/pb", "rdc/protocol/pb"),
    ("src/rdc/ui/theme", "rdc/ui/theme"),
]
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = [
    # qasync 只在函数内延迟 import（ui/app.event_loop），分析器容易漏
    "qasync",
    # 同样属"函数内延迟导入"，分析器收不到：input/legacy_pyautogui._load_pyautogui
    # 是 SendInput 不可用时的回退输入后端（真打包实测：不加这两条，frozen 下
    # 回退路径静默抛 PlatformUnsupportedError —— 源码能跑、exe 不行，最难查）。
    "pyautogui",
    "pygetwindow",
    "mouseinfo",
    "pymsgbox",
    "pyscreeze",
    # structlog 处理器经字符串/惰性装配引用
    "structlog.dev",
    "structlog.processors",
    "structlog.stdlib",
    # 平台后端与第三方运行时：按平台/配置分派，静态分析看不到分支
    "bcrypt",
    "cryptography",
    "cryptography.hazmat.primitives.ciphers",
    "mss",
    "psutil",
    "PIL",
    "pyperclip",
]

for _mod in PYSIDE_MODULES:
    hiddenimports += collect_submodules(_mod)

# av / shiboken6：包内二进制非 import 可达，必须整体收集（见模块 docstring 4）
for _name in ("av", "shiboken6"):
    _datas, _binaries, _imports = collect_all(_name)
    datas += _datas
    binaries += _binaries
    hiddenimports += _imports

a = Analysis(
    ["src/rdc/desktop_entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 测试/开发工具绝不该进产物（pytest-qt 会把 Qt Test 模块整套拖进来）
        "pytest",
        "pytest_cov",
        "pytest_asyncio",
        "pytestqt",
        "hypothesis",
        "IPython",
        # v4 遗留：v5 源码零引用（全树 grep 实测）
        "tkinter",
        "pydoc_data",
        # Qt 大件：源码零引用，collect_all 阶段的 899 MB 元凶（见 docstring 3）
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngineProcess",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQml",
        "PySide6.QtDesigner",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtTest",
        "PySide6.QtSql",
        # 音频链路属阶段 8（未实现），v5.0.0 不打包
        "sounddevice",
        "pynput",  # 阶段 7 监听侧已剔除，仅 legacy 回退用；SendInput 为主路径
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WaltonDesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 见 docstring 要点 1，勿改
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="WaltonDesk",
)
