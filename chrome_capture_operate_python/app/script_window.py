"""托盘快速执行脚本的输出窗口（tkinter，标准库零依赖）。

为什么不用 cmd 控制台窗口：新控制台默认 GBK 代码页，Python UTF-8
输出中文易乱码（chcp 65001 也非所有环境可靠）；且窗口行为（保留/
关闭）不受控。改用 tkinter 独立窗口：

- 子进程输出走管道（PYTHONIOENCODING=utf-8），窗口内 Text 以 UTF-8
  解码显示——无代码页问题；
- 实时滚动显示 stdout/stderr（stderr 前缀标记）；
- 脚本执行完毕后窗口保留（需求），标题显示"已完成+退出码"，用户
  手动关闭；窗口右上角关闭即结束（脚本仍在跑则先终止进程）；
- 每次托盘点击开新窗口（独立进程跑 Tk，主程序 pythonw 无影响）。

窗口以独立进程运行（python -m app.script_window -- 脚本路径）：
Tk 主循环与 uvicorn 事件循环分属不同进程，互不阻塞；窗口进程退出
不影响服务。
"""
import os
import subprocess
import sys
import threading

# 与 executor 相同的子进程环境：UTF-8 输出 + 屏蔽告警
ENV = dict(os.environ, PYTHONIOENCODING="utf-8",
           PYTHONWARNINGS="ignore")

MAX_CHARS = 200000  # 输出上限（防超长输出拖垮 Text 控^）


def _venv_python():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(here, ".venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else sys.executable


def launch(script_path):
    """启动输出窗口进程（独立 python 跑 Tk 主循环）。

    由托盘菜单点击调用（run_script_console）——返回 Popen 的窗口
    进程，窗口内自行执行脚本并展示输出。
    """
    return subprocess.Popen(
        [_venv_python(), "-m", "app.script_window", "--", script_path],
        cwd=os.path.dirname(os.path.abspath(__file__)) and
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=ENV,
        creationflags=subprocess.CREATE_NO_WINDOW)


def _run_in_window(script_path):
    """窗口进程入口：Tk 窗口 + 后台线程跑脚本实时回填。"""
    import tkinter as tk
    from tkinter import scrolledtext

    name = os.path.basename(os.path.dirname(script_path)) + "/" + \
        os.path.basename(script_path)

    root = tk.Tk()
    root.title("脚本执行 - %s" % name)
    root.geometry("760x480")
    root.minsize(480, 260)

    status = tk.StringVar(value="运行中…")
    bar = tk.Frame(root)
    bar.pack(fill=tk.X, padx=8, pady=(8, 0))
    tk.Label(bar, textvariable=status, fg="#444").pack(side=tk.LEFT)

    text = scrolledtext.ScrolledText(
        root, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED,
        bg="#111318", fg="#d8dee9")
    text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    proc = subprocess.Popen(
        [_venv_python(), "-u", script_path],
        cwd=os.path.dirname(script_path), env=ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)

    def append(s):
        # 线程安全：Tk 的 insert 经 after 投递到主循环
        def _do():
            text.config(state=tk.NORMAL)
            text.insert(tk.END, s)
            # 超长裁剪头部（保留尾部）
            if text.index("end-1c") > text.index(
                    "1.0 +%dc" % MAX_CHARS):
                text.delete("1.0", "end-1c -%dc" % MAX_CHARS)
            text.see(tk.END)
            text.config(state=tk.DISABLED)
        root.after(0, _do)

    def drain(stream, prefix=""):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace")
            append(prefix + line)
        stream.close()

    def worker():
        t_out = threading.Thread(target=drain, args=(proc.stdout,),
                                 daemon=True)
        t_err = threading.Thread(target=drain, args=(proc.stderr, "[stderr] "),
                                 daemon=True)
        t_out.start()
        t_err.start()
        code = proc.wait()
        t_out.join(timeout=3)
        t_err.join(timeout=3)
        # 执行完毕：窗口保留（需求），状态栏提示退出码
        append("\n[执行完毕，退出码 %s。窗口可关闭]\n" % code)
        root.after(0, lambda: status.set(
            "已完成（退出码 %s）——输出保留，关闭窗口即结束" % code))

    threading.Thread(target=worker, daemon=True).start()

    def on_close():
        # 脚本仍在跑：用户关窗视作放弃执行，终止进程
        if proc.poll() is None:
            proc.kill()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--" and len(args) > 1:
        _run_in_window(args[1])


if __name__ == "__main__":
    main()
