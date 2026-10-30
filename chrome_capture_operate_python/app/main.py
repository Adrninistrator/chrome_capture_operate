"""唯一启动入口：防重复启动 -> 日志 -> 系统托盘线程 -> uvicorn 主线程。

start.bat 与 PyCharm 都运行本模块（start.bat 只负责选择解释器）：
- 启动成功：/health 就绪后右下角弹出自动关闭提示；
- 启动失败/已在运行/端口被占用：同样弹出自动关闭提示（不阻塞）；
- `.venv` 缺失时 start.bat 用系统 Python 运行 `app.main --venv-check`，
  由本模块弹出“请先运行 install.bat”提示。
"""
import logging
import os
import socket
import sys
import threading

# 支持两种启动方式：
#   python -m app.main   （start.bat 使用，推荐）
#   python app/main.py   （直接运行脚本时，补齐包上下文）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    __package__ = "app"

from . import chrome_proc  # noqa: E402
from .config import (Config, LOG_DIR, ensure_dirs,  # noqa: E402
                     ensure_auto_start)
from .logutil import setup_logging  # noqa: E402

log = logging.getLogger("app")


def already_running(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def msgbox(text, title="chrome_capture_operate"):
    """右下角自动关闭提示（10 秒），不阻塞、无需点击。

    start.bat 与 PyCharm 使用同一入口 app.main，因此启动失败/已在运行
    的提示行为一致。提示由独立进程 tkinter 窗口呈现，主程序可立即退出。
    """
    try:
        from . import popup_notify
        popup_notify.notify(title, text, 10)
    except Exception as e:
        log.warning("启动提示弹窗失败: %s", e)


def _health_ok(port):
    """本程序的 /health 是否可访问（区分“已在运行”与端口被其他程序占用）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % port, timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _startup_notifier(port):
    """等待本程序 /health 就绪后提示启动成功；失败由 main 的错误路径提示。"""
    import time as _time
    import urllib.request
    from . import popup_notify
    url = "http://127.0.0.1:%d" % port
    deadline = _time.time() + 20
    while _time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=0.5) as r:
                if r.status == 200:
                    popup_notify.notify(
                        "启动成功", "chrome_capture_operate 已启动：%s" % url, 6)
                    return
        except Exception:
            pass
        _time.sleep(0.2)
    popup_notify.notify(
        "启动失败", "主程序在 20 秒内未就绪\n请查看 log 目录", 10)


def _tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), (32, 64, 128))
    d = ImageDraw.Draw(img)
    d.ellipse((14, 14, 50, 50), fill=(80, 200, 255))
    return img


def run_script_console(script_path):
    """弹独立输出窗口执行生成的脚本（托盘快速执行菜单）。

    prompt 需求「系统托盘」：点击后执行，显示能展示输出、执行完毕后
    保留的窗口。改用 tkinter 自绘窗口（app/script_window.py 独立进程）
    替代 cmd 控制台：cmd 新窗口默认 GBK 代码页，Python UTF-8 输出中文
    乱码（chcp 也不可靠）；tk 窗口以管道捕获 + UTF-8 解码显示，无代码
    页问题，且窗口保留/关闭行为完全受控（脚本未结束时关窗即终止进程）。
    """
    from . import script_window
    log.info("托盘快速执行脚本(输出窗口): %s", script_path)
    return script_window.launch(script_path)


def _tray_scripts():
    """托盘快速执行脚本列表（全局配置 tray_scripts）。"""
    from . import globalconf
    return globalconf.get_json(globalconf.KEY_TRAY_SCRIPTS, [])


def _tray_label(script_path):
    """托盘菜单脚本标签：{最底层子目录名/脚本名}（prompt 需求）。

    如 python_scripts_example/health_check/main.py 显示
    "health_check/main.py"——目录名区分同名脚本，比裸文件名可读。
    """
    d = os.path.basename(os.path.dirname(script_path))
    f = os.path.basename(script_path)
    return "%s/%s" % (d, f) if d else f


def build_tray_menu(port, icon, on_quit):
    """构造托盘菜单：快速执行脚本（动态子菜单）+ 打开页面 + 退出。"""
    import pystray
    scripts = _tray_scripts()
    if scripts:
        sub = pystray.Menu(*[
            pystray.MenuItem(
                _tray_label(p),
                (lambda path: lambda: run_script_console(path))(p))
            for p in scripts if os.path.isfile(p)])
        quick = pystray.MenuItem("快速执行脚本", sub)
    else:
        quick = pystray.MenuItem(
            "快速执行脚本（未配置，见Web页\"快速执行脚本\"）", None)
    return pystray.Menu(
        quick,
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "打开页面",
            lambda: chrome_proc.open_in_chrome(
                "http://127.0.0.1:%d" % port),
            default=True),  # 双击托盘图标也打开页面
        pystray.MenuItem("退出", lambda: on_quit(icon)),
    )


def start_tray(port, on_quit):
    import pystray
    icon = pystray.Icon(
        "chrome_capture_operate", _tray_image(), "chrome_capture_operate")
    icon.menu = build_tray_menu(port, icon, on_quit)

    # Web 页增删托盘脚本后回调（经 webapp tray_on_change 注入）：
    # 重建菜单（rebuild_menu 挂在 icon 上供外部线程调用）。
    def rebuild_menu():
        # pystray 跨线程更新：先替换 icon.menu 再 update_menu()，
        # 变更会被投递到托盘线程重绘（run_detached 需在 icon 线程内，
        # 外部线程直接调用 update_menu 即可）
        icon.menu = build_tray_menu(port, icon, on_quit)
        icon.update_menu()

    icon._rebuild_menu = rebuild_menu
    icon.run()


def _start_tray_with_holder(port, on_quit, holder):
    """在托盘线程启动 start_tray；icon 对象建立后登记进 holder，
    供 Web 页增删脚本后回调重建菜单。"""
    import pystray

    original_icon_cls = pystray.Icon

    class _HookedIcon(original_icon_cls):
        def run(self):
            holder["icon"] = self
            original_icon_cls.run(self)

    # 临时替换：start_tray 内部构造的 icon 即为 hook 实例
    pystray.Icon = _HookedIcon
    try:
        start_tray(port, on_quit)
    finally:
        pystray.Icon = original_icon_cls


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # start.bat 在 .venv 缺失时用系统 Python 运行本模式：只弹提示并退出，
    # 不导入第三方依赖（此时 venv 不可用）。
    if "--venv-check" in argv:
        # 此处尚未执行 setup_logging（其返回值绑定到 main 局部变量 log），
        # 使用模块级 logger，避免 UnboundLocalError
        logging.getLogger("app").error("启动失败：.venv 不存在或不完整")
        msgbox(".venv 不存在或不完整\n请先运行 install.bat 安装依赖",
               "启动失败")
        sys.exit(1)
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ensure_dirs()
    log, log_path = setup_logging()
    config = Config()
    port = int(config.get("port"))

    # 依赖前置检查：mcp 库缺失直接终止启动（pythonw 无控制台，错误经
    # 日志与弹窗呈现；install.bat 装依赖后即可正常启动）
    try:
        import mcp  # noqa: F401
    except ImportError as e:
        msg = ("chrome_capture_operate 启动失败：未安装 mcp 依赖库（%s）。\n"
               "请先执行 chrome_capture_operate_python 目录下的 "
               "install.bat 安装依赖，完成后重新启动本程序" % e)
        log.error(msg)
        msgbox(msg)
        sys.exit(1)

    if already_running(port):
        if _health_ok(port):
            log.error("端口 %d 已被本程序占用，已在运行，不支持重复启动", port)
            msgbox("chrome_capture_operate 已在运行（端口 %d），不支持重复启动"
                   % port)
            sys.exit(0)
        log.error("端口 %d 已被其他程序占用", port)
        msgbox("端口 %d 已被其他程序占用，请检查后重试" % port)
        sys.exit(1)

    # 自启动同步：参数值（是否自启动）在全局配置文件（需求），注册表为
    # 落地实现。启动时按参数值同步注册表：失效/误删时按当前路径重建，
    # 指向其他有效副本时尊重不抢，未启用则清除（详见 ensure_auto_start）。
    if ensure_auto_start():
        log.info("开机自启动已启用")

    from .logutil import uvicorn_log_config
    from .webapp import create_app
    import uvicorn

    tray_holder = {}

    def tray_on_change():
        """Web 页增删托盘脚本后重建托盘菜单（托盘线程内执行）。"""
        icon = tray_holder.get("icon")
        if icon and getattr(icon, "_rebuild_menu", None):
            try:
                icon._rebuild_menu()
            except Exception as e:
                log.warning("重建托盘菜单失败: %s", e)

    def tray_notify(message):
        """抓包开始/结束的托盘气泡通知（prompt 需求，Web 与 MCP 触发
        均经 capture 状态机回调至此）。托盘图标尚未就绪/已退出时静默
        跳过；icon.notify 沿用 rebuild_menu 同款跨线程调用。"""
        icon = tray_holder.get("icon")
        if icon is None:
            return
        try:
            icon.notify(message, "chrome_capture_operate")
        except Exception as e:
            log.warning("托盘气泡通知失败: %s", e)

    app = create_app(tray_on_change=tray_on_change, tray_notify=tray_notify)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_config=uvicorn_log_config(log_path), access_log=False))

    def on_quit(icon):
        log.info("托盘退出")
        server.should_exit = True
        icon.stop()
        # 兜底：给 uvicorn 3 秒优雅退出时间
        threading.Timer(3, lambda: os._exit(0)).start()

    # start_tray 内把 icon 注册到 holder（经 _TrayHook）
    tray = threading.Thread(target=_start_tray_with_holder,
                            args=(port, on_quit, tray_holder),
                            daemon=True)
    tray.start()

    # 安装/卸载完成通知（需求「统一要求」：安装完毕让客户知道）：
    # ext_install 任务完成/失败时在桌面右下角弹出 30 秒后自动消失的
    # 窗口（popup_notify，独立线程 tkinter 窗口，非阻塞）——无论从
    # Web 页还是 AI/MCP 触发，用户不盯着页面也能知道结果
    from . import ext_install, popup_notify
    ext_install.set_notify(popup_notify.notify)

    log.info("启动完成: http://127.0.0.1:%d (log: %s)", port, LOG_DIR)

    # prompt 需求"启动后程序处理"：启动后用**日常使用的 Chrome**打开
    # Web 页面（即使 Chrome 不是默认浏览器也要用 Chrome，chrome_proc.
    # open_in_chrome 实现，托盘"打开页面"共用）；找不到 Chrome 时才退回
    # 默认浏览器。延迟到 server.run() 之后由线程触发：uvicorn 起监听
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
            chrome_proc.open_in_chrome(url)
        except Exception as e:  # 打开失败不影响服务运行
            log.warning("自动打开浏览器失败: %s", e)

    threading.Thread(target=_open_browser_after_ready, daemon=True).start()
    # 启动结果提示（与 start.bat 无关，PyCharm 直接运行同一入口也有）
    threading.Thread(target=_startup_notifier, args=(port,),
                     daemon=True).start()

    try:
        server.run()
    except OSError as e:
        logging.getLogger("app").error("服务启动失败: %s", e)
        msgbox("服务启动失败：%s\n（端口 %d 可能被占用）" % (e, port))
        sys.exit(1)


if __name__ == "__main__":
    main()
