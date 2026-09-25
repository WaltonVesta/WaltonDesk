"""PySide6 UI 层（阶段 6）。

红线：**禁止任何跨线程控件操作**。本包全部 widget 的公开写入口经
:func:`rdc.ui.app.ensure_gui_thread` 守卫；asyncio 侧（qasync 下与 Qt 同主线程）
更新 UI 走 Signal/Slot，worker 线程（pipeline executor）只回传数据，绝不触控件。
"""
