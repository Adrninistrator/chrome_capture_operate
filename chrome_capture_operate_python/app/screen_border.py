# -*- coding: utf-8 -*-
"""安装/卸载执行期间桌面周边绿色边框（需求「统一要求」）。

类似腾讯会议录屏的桌面四周指示：开始安装后显示，操作完毕时结束——
提示用户自动化操作进行中（期间请勿操作键鼠）。

实现：**独立进程** tkinter 全屏置顶无边框窗口，中间区域为透明色
（-transparentcolor），四边绘制绿色边框；窗口设为点击穿透
（WS_EX_TRANSPARENT | WS_EX_LAYERED），不影响任何鼠标键盘操作；
范围取虚拟屏幕（GetSystemMetrics，覆盖多显示器）。

为何独立进程（而非服务进程内线程）：tkinter 在非主线程创建/销毁、
再经 GC 跨线程终结 Tcl 对象会引发原生崩溃（实测 Windows fatal
exception 0x80000003，可击垮整个服务进程）——进程退出即清理，
与 app/script_window.py（托盘脚本输出窗口）同款隔离模式。

用法（服务内）：show() 启动边框进程（幂等）；hide() 终止。
子进程入口：python -m app.screen_border（最长 15 分钟自动退出，
防父进程异常退出后边框残留）。
"""
import logging
import os
import subprocess
import sys
import threading

log = logging.getLogger("app.screen_border")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAX_SEC = 15 * 60      # 安全自超时：父进程异常退出时防边框永久残留

_lock = threading.Lock()
_proc = {"p": None}


def _spawn():
    """启动边框子进程（CREATE_NO_WINDOW：python.exe 不闪控制台）。"""
    return subprocess.Popen(
        [sys.executable, "-m", "app.screen_border"],
        cwd=BASE_DIR, creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def show():
    """显示桌面周边绿色边框（非阻塞；已显示时幂等）。"""
    with _lock:
        p = _proc["p"]
        if p is not None and p.poll() is None:
            return
        try:
            _proc["p"] = _spawn()
        except OSError as e:
            log.warning("启动绿色边框进程失败: %s", e)


def hide():
    """结束绿色边框（终止子进程；未显示时为空操作）。"""
    with _lock:
        p = _proc["p"]
        _proc["p"] = None
    if p is None:
        return
    try:
        p.terminate()
        p.wait(2)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


# ---------------- 子进程入口：窗口本体 ----------------

def _window_main():
    """边框窗口（独立进程中运行，直到被终止或安全超时）。"""
    import tkinter as tk
    x, y, w, h = _virtual_screen()
    root = tk.Tk()
    root.overrideredirect(True)          # 无边框无标题栏
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", _TRANSPARENT)
    root.geometry("+%d+%d" % (x, y))
    canvas = tk.Canvas(root, width=w, height=h, bg=_TRANSPARENT,
                       highlightthickness=0)
    canvas.pack()
    # 四周绿色边框（内缩半个线宽，保证全屏四周可见）
    pad = _BORDER_WIDTH // 2
    canvas.create_rectangle(pad, pad, w - pad, h - pad,
                            outline=_BORDER_COLOR,
                            width=_BORDER_WIDTH)
    # 安全自超时：父进程异常退出后防边框永久残留
    root.after(_MAX_SEC * 1000, root.destroy)
    root.update_idletasks()
    _make_click_through(root)
    root.mainloop()


_TRANSPARENT = "#010203"     # 透明色（近黑自定义色，不与边框配色冲突）
_BORDER_COLOR = "#00e05a"    # 边框颜色（腾讯会议录屏同款绿）
_BORDER_WIDTH = 8


def _virtual_screen():
    """虚拟屏幕范围（多显示器整体）：返回 (x, y, w, h)。"""
    import ctypes
    u = ctypes.windll.user32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (u.GetSystemMetrics(SM_XVIRTUALSCREEN),
            u.GetSystemMetrics(SM_YVIRTUALSCREEN),
            u.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            u.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def _make_click_through(root):
    """置点击穿透（WS_EX_TRANSPARENT）：边框不拦截任何鼠标操作。"""
    import ctypes
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    try:
        u = ctypes.windll.user32
        hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
        style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                         style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception as e:
        log.warning("设置点击穿透失败（不影响边框显示）: %s", e)


def main():
    _window_main()


if __name__ == "__main__":
    main()
