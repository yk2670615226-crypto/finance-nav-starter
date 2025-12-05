import platform
import platform
import sys
from pathlib import Path


def get_base_path() -> Path:
    """获取资源路径（适配 PyInstaller 打包）"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
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
