#!/usr/bin/env python3
"""本地启动器：以 -I 运行时显式加入本脚本所在目录（应用代码目录）。

资料目录通过参数传入，不加入 sys.path，避免资料中的同名模块被导入。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reading_app.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
