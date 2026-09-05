"""入口：防重复启动 -> 日志 -> 系统托盘线程 -> uvicorn 主线程。

start.bat 以 pythonw 启动，无控制台；错误通过日志与弹窗呈现。
"""
import ctypes
import logging
import os
import socket
import sys
import threading
import webbrowser

# 支持两种启动方式：
#   python -m app.main   （start.bat 使用，推荐）
#   python app/main.py   （直接运行脚本时，补齐包上下文）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    __package__ = "app"

from .config import (Config, LOG_DIR, ensure_dirs,  # noqa: E402
                     ensure_auto_start)
from .logutil import setup_logging  # noqa: E402


def already_running(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def msgbox(text, title="chrome_capture_operate"):
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        pass


def _tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), (32, 64, 128))
    d = ImageDraw.Draw(img)
    d.ellipse((14, 14, 50, 50), fill=(80, 200, 255))
    return img


def start_tray(port, on_quit):
    import pystray
    icon = pystray.Icon(
        "chrome_capture_operate", _tray_image(), "chrome_capture_operate",
        menu=pystray.Menu(
            pystray.MenuItem(
                "打开页面",
                lambda: webbrowser.open("http://127.0.0.1:%d" % port),
                default=True),  # 双击托盘图标也打开页面
            pystray.MenuItem("退出", lambda: on_quit(icon)),
        ))
    icon.run()


def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ensure_dirs()
    log, log_path = setup_logging()
    config = Config()
    port = int(config.get("port"))

    if already_running(port):
        log.error("端口 %d 已被占用，程序已在运行，不支持重复启动", port)
        msgbox("chrome_capture_operate 已在运行（端口 %d），不支持重复启动"
               % port)
        sys.exit(1)

    # 自启动同步：参数值（是否自启动）在全局配置文件（需求），注册表为
    # 落地实现。启动时按参数值同步注册表：失效/误删时按当前路径重建，
    # 指向其他有效副本时尊重不抢，未启用则清除（详见 ensure_auto_start）。
    if ensure_auto_start():
        log.info("开机自启动已启用")

    from .logutil import uvicorn_log_config
    from .webapp import create_app
    import uvicorn

    app = create_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_config=uvicorn_log_config(log_path), access_log=False))

    def on_quit(icon):
        log.info("托盘退出")
        server.should_exit = True
        icon.stop()
        # 兜底：给 uvicorn 3 秒优雅退出时间
        threading.Timer(3, lambda: os._exit(0)).start()

    tray = threading.Thread(target=start_tray, args=(port, on_quit),
                            daemon=True)
    tray.start()
    log.info("启动完成: http://127.0.0.1:%d (log: %s)", port, LOG_DIR)

    # prompt.md"启动后程序处理"：启动后用默认浏览器（日常使用的 Chrome）
    # 打开 Web 页面。延迟到 server.run() 之后由线程触发：uvicorn 起监听
    # 需要片刻，直接开可能连不上；线程里先探活端口再打开。
    def _open_browser_after_ready():
        import time as _time
        url = "http://127.0.0.1:%d" % port
        for _ in range(50):  # 最多等 5 秒
            try:
                with socket.create_connection(("127.0.0.1", port),
                                              timeout=0.2):
                    break
            except OSError:
                _time.sleep(0.1)
        try:
            webbrowser.open(url)
            log.info("已在默认浏览器打开页面: %s", url)
        except Exception as e:  # 打开失败不影响服务运行
            log.warning("自动打开浏览器失败: %s", e)

    threading.Thread(target=_open_browser_after_ready, daemon=True).start()

    try:
        server.run()
    except OSError as e:
        logging.getLogger("app").error("服务启动失败: %s", e)
        msgbox("服务启动失败：%s\n（端口 %d 可能被占用）" % (e, port))
        sys.exit(1)


if __name__ == "__main__":
    main()
