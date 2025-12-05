import socket
import sys
import threading
import time
import urllib.request
import webview
from typing import Optional

from app import app, socketio

HOST = "127.0.0.1"
WINDOW_TITLE = "个人记账系统 Pro"


def get_free_port() -> int:
    """获取本机闲置端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def wait_for_server(port: int, timeout: float = 8.0) -> bool:
    """轮询检查后端是否就绪"""
    url = f"http://{HOST}:{port}/"
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(url) as response:
                if response.getcode() == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def start_server(port: int):
    """启动 Flask-SocketIO 服务"""
    socketio.run(
        app, 
        host=HOST, 
        port=port, 
        debug=False, 
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )


def main() -> Optional[int]:
    port = get_free_port()
    url = f"http://{HOST}:{port}/"
    print(f"系统启动中... {url}")
    
    # 启动后端线程
    t = threading.Thread(target=start_server, args=(port,), daemon=True)
    t.start()

    if not wait_for_server(port):
        print("错误：后端服务器启动超时。")
        return 1

    # 初始化 WebView
    webview.create_window(
        title=WINDOW_TITLE,
        url=url,
        width=1600,
        height=900,
        min_size=(1024, 768),
        resizable=True,
        confirm_close=True,
        text_select=False 
    )
    
    # 启动 GUI (private_mode=False 确保 Cookie 可保存)
    webview.start(private_mode=False, storage_path=None)
    return None


if __name__ == "__main__":
    sys.exit(main())