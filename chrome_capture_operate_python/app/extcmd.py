# -*- coding: utf-8 -*-
"""经命令页在无 CDP 的日常 Chrome 中执行插件指令（读取/修改配置）。

改编自 docs/chrome插件网页修改配置/ 的 ext_cmd 实测方案（2026-09-23
日常浏览器全链路验证），区别：本服务自身常驻 127.0.0.1，无需临时
http.server——命令页由本服务路由 /extension-cmd 托管（origin 即满足
插件 externally_connectable 白名单）。

流程（run_ext_command 阻塞等待，供 Web/MCP 调用）：
1) 生成一次性 token，chrome.exe 打开
   http://127.0.0.1:{port}/extension-cmd?token=..&cmd=..&参数
   ——进程转交给运行中的日常 Chrome 开新标签页（无 CDP 也可达）；
2) 命令页 JS 经 externally_connectable 消息通道执行 get_config/
   set_config（含回读验证），结果 POST 回 /api/extension/cmd_report；
3) 本模块 report() 记录结果并唤醒等待方，run_ext_command 返回结果
   dict（超时返回错误）。

注意：每次调用会在日常 Chrome 留一个命令页标签（页面会尝试自关，
通常被 Chrome 拦截；可定期手工清理——docs 同款行为）。
"""
import logging
import subprocess
import threading
import time
import uuid
from urllib.parse import quote

log = logging.getLogger("app.extcmd")

# 一次性回传上下文：token -> {"event": Event, "result": dict|None}
_pending = {}
_lock = threading.Lock()


def run_ext_command(port, cmd, params=None, timeout=20, log_fn=None):
    """执行插件指令并等待命令页回传结果（阻塞，线程中调用）。

    cmd 为 get_config / set_config（open_options 亦可）；params 为
    命令页 URL 查询参数 dict（如 set_config 的 push_scope/allow/deny）。
    返回命令页回传的结果 dict；失败/超时返回 {"ok": False, "error": ...}。
    """
    _log = log_fn or (lambda m: log.info(m))
    token = uuid.uuid4().hex
    with _lock:
        _pending[token] = {"event": threading.Event(), "result": None}
    try:
        qs = ["token=" + token, "cmd=" + quote(cmd, safe="")]
        for k, v in (params or {}).items():
            if v is not None:
                qs.append(quote(str(k), safe="") + "=" + quote(str(v), safe=""))
        url = "http://127.0.0.1:%d/extension-cmd?%s" % (port, "&".join(qs))
        from . import chrome_proc
        exe = chrome_proc.find_chrome()
        if not exe:
            return {"ok": False,
                    "error": "未找到 Chrome 安装路径（注册表与常见路径均未命中），命令页无法打开"}
        _log("打开命令页（日常 Chrome 新标签页，自动执行并回传）: %s" % url)
        try:
            subprocess.Popen([exe, url],
                             creationflags=subprocess.DETACHED_PROCESS
                             | subprocess.CREATE_NEW_PROCESS_GROUP,
                             close_fds=True)
        except OSError as e:
            return {"ok": False, "error": "打开命令页失败: %s" % e}
        ctx = _pending[token]
        if not ctx["event"].wait(timeout):
            return {"ok": False,
                    "error": ("等待命令页回传超时（%d 秒）——Chrome 未运行、"
                              "页面未完成或插件未安装" % timeout)}
        result = ctx["result"]
        return result if isinstance(result, dict) else {
            "ok": False, "error": "命令页回传内容异常"}
    finally:
        with _lock:
            _pending.pop(token, None)


def report(token, result):
    """命令页结果回传（webapp /api/extension/cmd_report 调用）。

    token 匹配等待中的调用时记录结果并唤醒等待方，返回 True；
    token 无效（过期/不存在/重复）返回 False。
    """
    with _lock:
        ctx = _pending.get(token)
        if not ctx or ctx["result"] is not None:
            return False
        ctx["result"] = result
        ctx["event"].set()
        return True


def pending_count():
    """等待中的命令数（状态观测/测试用）。"""
    with _lock:
        return len(_pending)
