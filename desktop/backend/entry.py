"""PyInstaller 打包入口 —— 把整个 Python 引擎打成单文件可执行程序。

这个文件是 Electron 外壳与 Python 引擎之间的"打包边界"：
Electron 只需要知道"启动这个二进制、监听某个端口"，不关心内部实现。

打包命令见 desktop/scripts/build-backend.sh
"""
from __future__ import annotations

import multiprocessing
import os
import sys


def _setup_frozen_paths() -> None:
    """PyInstaller 冻结后，把打包进来的资源目录加进搜索路径。

    PyInstaller 会把 --add-data 的内容解压到 sys._MEIPASS 下。
    这里把 src/ 加进 sys.path，让 `import src.server` 能正常工作。
    """
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    for candidate in (base, os.path.join(base, "src")):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


def main() -> None:
    _setup_frozen_paths()

    # 冻结环境下不要自动开浏览器（由 Electron 管窗口）
    os.environ.setdefault("WXBOT_NO_BROWSER", "1")

    from src.server import main as server_main

    server_main()


if __name__ == "__main__":
    # PyInstaller + 多进程需要，防止子进程重复启动
    multiprocessing.freeze_support()
    main()
