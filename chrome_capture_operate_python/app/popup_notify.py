# -*- coding: utf-8 -*-
"""桌面右下角弹窗通知（需求「统一要求」：安装完毕让客户知道）。

在桌面右下角弹出一个窗口，默认 30 秒后自动消失（点击窗口可提前
关闭）。

实现：**独立进程** tkinter 窗口（置顶、右下角定位、深蓝配色、正文
自动换行）——tkinter 在非主线程创建/销毁、再经 GC 跨线程终结 Tcl
对象会引发原生崩溃（实测 Windows fatal exception 0x80000003，可击垮
整个服务进程），故与服务进程隔离：进程退出即清理，与
app/script_window.py 同款模式。notify() 对调用方非阻塞（fire-and-
forget），返回子进程 Popen 供测试等待。

子进程入口：python -m app.popup_notify <title> <message> <timeout_sec>
"""
import logging
import os
import subprocess
import sys

log = logging.getLogger("app.popup_notify")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def notify(title, message, timeout_sec=30):
    """桌面右下角弹窗（非阻塞）；返回子进程 Popen（测试可 wait）。"""
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "app.popup_notify",
             str(title), str(message), str(timeout_sec)],
            cwd=BASE_DIR, creationflags=subprocess.CREATE_NO_WINDOW,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    except OSError as e:
        log.warning("弹窗通知启动失败: %s", e)
        log.info("弹窗通知（降级日志）: %s | %s", title, message)
        return None


def main():
    """子进程入口：argv = [title, message, timeout_sec]。"""
    import tkinter as tk
    title = sys.argv[1] if len(sys.argv) > 1 else "chrome_capture_operate"
    message = sys.argv[2] if len(sys.argv) > 2 else ""
    timeout_sec = 30
    try:
        timeout_sec = float(sys.argv[3]) if len(sys.argv) > 3 else 30
    except ValueError:
        pass
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    # 与服务一致的深蓝配色；正文自动换行（宽消息不超出屏幕）
    lbl = tk.Label(root, text=message, wraplength=420, justify="left",
                   font=("Microsoft YaHei", 11), padx=18, pady=14)
    root.configure(bg="#1f3a5f")
    lbl.configure(bg="#1f3a5f", fg="#ffffff")
    lbl.pack()
    # 右下角定位（底部预留任务栏高度）
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("+%d+%d" % (sw - w - 16, sh - h - 56))
    # 30 秒自动消失；点击提前关闭（窗口本身非模态）
    root.after(int(timeout_sec * 1000), root.destroy)
    root.bind("<Button-1>", lambda e: root.destroy())
    root.mainloop()


if __name__ == "__main__":
    main()
