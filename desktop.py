import sys
import threading
import time
import socket
import urllib.request
from typing import Optional

import webview

from app import socketio, app

HOST = "127.0.0.1"


def get_free_port() -> int:
    """在本机随机申请一个可用端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def wait_for_server(port: int, timeout: float = 5.0) -> bool:
    """
    轮询检查后端服务是否已启动成功。

    :param port: Flask 后端端口
    :param timeout: 最大等待时间（秒）
    :return: 启动成功返回 True，超时返回 False
    """
    start_time = time.time()
    url = f"http://{HOST}:{port}/"

    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(url) as response:
                if response.getcode() == 200:
                    return True
        except Exception:
            time.sleep(0.1)

    return False


def start_flask(port: int) -> None:
    """在子线程中启动 Flask-SocketIO 服务器。"""
    socketio.run(
        app,
        host=HOST,
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )


def main() -> Optional[int]:
    """桌面程序入口：启动后端 + 打开 PyWebView 窗口。"""
    # 1. 动态获取端口
    port = get_free_port()
    url = f"http://{HOST}:{port}/"
    print(f"系统启动中... {url}")

    # 2. 启动后端服务（后台线程）
    flask_thread = threading.Thread(target=start_flask, args=(port,), daemon=True)
    flask_thread.start()

    # 3. 等待后端就绪
    if not wait_for_server(port):
        print("错误：后端服务器启动超时。")
        return 1

    # 4. 创建桌面窗口
    webview.create_window(
        title="个人记账系统 Pro",
        url=url,
        width=1600,
        height=900,
        min_size=(1000, 700),
        resizable=True,
        confirm_close=True,
        text_select=False,
    )

    # 5. 启动 GUI（非隐私模式，保留 Cookie）
    webview.start(private_mode=False)
    return None


if __name__ == "__main__":
    exit_code = main()
    if exit_code is not None:
        sys.exit(exit_code)
