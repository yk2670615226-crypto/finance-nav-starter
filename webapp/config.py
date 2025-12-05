"""运行环境相关的路径工具，兼容源码与打包模式。"""

import platform
import sys
from pathlib import Path


def get_base_path() -> Path:
    """获取资源路径（适配 PyInstaller 打包）"""
    if getattr(sys, "frozen", False):
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            return Path(frozen_root)
        return Path(sys.executable).parent
    return Path(__file__).parent.parent.absolute()


def get_data_path() -> Path:
    """获取数据存储路径（适配各操作系统）"""
    if getattr(sys, "frozen", False):
        home = Path.home()
        system = platform.system()
        if system == "Windows":
            base = home / "AppData" / "Roaming"
        elif system == "Darwin":
            base = home / "Library" / "Application Support"
        else:
            base = home / ".local" / "share"

        data_dir = base / "MyAccounting"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    return Path(__file__).parent.parent.absolute()
