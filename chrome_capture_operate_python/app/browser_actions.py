"""通用浏览器动作执行器。

生成的自动登录脚本通过 HTTP 调用本模块，不直接依赖 Playwright；
Playwright 只在主应用内部使用，连接已运行的抓包 Chrome。

动作中的账号值使用 ``value_from`` 指定全局配置 ``login_accounts`` 的参数名称，
由主应用解析；生成脚本中不出现具体账号值。
"""
import ctypes
import logging
import os
import threading
import time
from urllib.parse import urlsplit

from . import chrome_proc

log = logging.getLogger("app.browser")

MAX_ACTIONS = 100
DEFAULT_ACTION_TIMEOUT_MS = 15000
DEFAULT_TOTAL_TIMEOUT_MS = 60000

# activate_page=False 时，页面登录/跳转仍可能被 Chrome 或页面 JS 激活窗口。
# 此处记录调用前的前台窗口并在关键节点还回去；Windows 前台锁定用「模拟
# 一次 ALT」解除（与 chrome_proc.bring_chrome_to_front 相同的成熟技巧）。
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002
_CHROME_WND_CLASS = "Chrome_WidgetWin_1"
_MIN_RESTORE_INTERVAL = 0.5      # 秒，避免反复激活时频繁模拟 ALT 干扰键盘

if os.name == "nt":
    _U32 = ctypes.windll.user32
    _U32.GetForegroundWindow.restype = ctypes.c_void_p
    _U32.IsWindow.argtypes = [ctypes.c_void_p]
    _U32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
else:
    _U32 = None

_last_restore = [0.0]

ALLOWED_ACTIONS = {
    "goto", "reload", "fill", "click", "press", "wait_for_selector",
    "wait_for_url", "wait_for_load_state", "wait_for_timeout", "get_url",
    "get_text", "get_title", "is_visible",
}
_LOAD_STATES = {"load", "domcontentloaded", "networkidle"}
_SELECTOR_STATES = {"attached", "detached", "visible", "hidden"}

# 浏览器动作锁按 CDP 端口：同一实例串行（避免多个请求互相切换标签页），
# 不同实例可并发登录（多账号多 CDP 实例方案 14.6）。
_BROWSER_LOCKS = {}
_BROWSER_LOCKS_GUARD = threading.Lock()


def _lock_for(cdp_port):
    with _BROWSER_LOCKS_GUARD:
        return _BROWSER_LOCKS.setdefault(int(cdp_port), threading.Lock())


def _foreground_hwnd():
    """返回当前前台窗口句柄；非 Windows 或异常返回 0。"""
    if _U32 is None:
        return 0
    try:
        return int(_U32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _is_chrome_window(hwnd):
    if _U32 is None or not hwnd:
        return False
    try:
        buf = ctypes.create_unicode_buffer(128)
        _U32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 128)
        return buf.value == _CHROME_WND_CLASS
    except Exception:
        return False


def _restore_foreground(hwnd):
    """activate_page=False 的兜底：仅当 Chrome 抢了前台时把焦点还回去。

    - 原窗口已关闭、前台已是原窗口时不动作；
    - 前台是用户自己切到的非 Chrome 窗口时不打扰（不强行拉回）；
    - 0.5 秒节流，避免页面反复激活时频繁模拟 ALT 干扰用户键盘。
    """
    if _U32 is None or not hwnd:
        return
    try:
        cur = int(_U32.GetForegroundWindow() or 0)
        if cur == hwnd or not _U32.IsWindow(ctypes.c_void_p(hwnd)):
            return
        if not _is_chrome_window(cur):
            return
        now = time.time()
        if now - _last_restore[0] < _MIN_RESTORE_INTERVAL:
            return
        _last_restore[0] = now
        _U32.keybd_event(_VK_MENU, 0, 0, 0)
        _U32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        _U32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


class BrowserActionError(Exception):
    """可返回给调用方的动作错误（不包含账号值）。"""

    def __init__(self, message, status_code=400, action_index=None,
                 action_type=None, url=None, results=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.action_index = action_index
        self.action_type = action_type
        self.url = url
        self.results = results or []

    def to_dict(self):
        d = {"ok": False, "error": self.message}
        if self.action_index is not None:
            d["action_index"] = self.action_index
        if self.action_type:
            d["action_type"] = self.action_type
        if self.url:
            d["url"] = self.url
        if self.results:
            d["results"] = self.results
        return d


def validate_actions(actions, accounts):
    """校验动作列表；不执行浏览器操作。"""
    if not isinstance(actions, list) or not actions:
        raise BrowserActionError("actions 不能为空")
    if len(actions) > MAX_ACTIONS:
        raise BrowserActionError("actions 过多（最多 %d 个）" % MAX_ACTIONS)
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            raise BrowserActionError("动作必须是对象", action_index=i)
        atype = str(a.get("type") or "").strip()
        if atype not in ALLOWED_ACTIONS:
            raise BrowserActionError("不支持的动作类型：%s" % (atype or "（空）"),
                                     action_index=i, action_type=atype)
        selector = a.get("selector")
        if atype in ("fill", "click", "press", "wait_for_selector",
                     "is_visible"):
            if not selector:
                raise BrowserActionError("%s 动作缺少 selector" % atype,
                                         action_index=i, action_type=atype)
        if atype == "fill":
            has_value = "value" in a and a.get("value") is not None
            has_from = bool(a.get("value_from"))
            if has_value and has_from:
                raise BrowserActionError(
                    "fill 动作不能同时指定 value 与 value_from",
                    action_index=i, action_type=atype)
            if not has_value and not has_from:
                raise BrowserActionError(
                    "fill 动作必须指定 value 或 value_from",
                    action_index=i, action_type=atype)
            if has_from and str(a.get("value_from")) not in accounts:
                raise BrowserActionError(
                    "参数名称不存在：%s" % a.get("value_from"),
                    action_index=i, action_type=atype)
        if atype == "press" and not a.get("key"):
            raise BrowserActionError("press 动作缺少 key",
                                     action_index=i, action_type=atype)
        if atype == "wait_for_selector":
            state = a.get("state")
            if state and state not in _SELECTOR_STATES:
                raise BrowserActionError(
                    "wait_for_selector 的 state 必须是 %s"
                    % "/".join(sorted(_SELECTOR_STATES)),
                    action_index=i, action_type=atype)
        if atype == "wait_for_url":
            if not a.get("contains") and not a.get("not_contains"):
                raise BrowserActionError(
                    "wait_for_url 动作必须指定 contains 或 not_contains",
                    action_index=i, action_type=atype)
        if atype == "wait_for_load_state":
            state = a.get("state") or "domcontentloaded"
            if state not in _LOAD_STATES:
                raise BrowserActionError(
                    "wait_for_load_state 的 state 必须是 %s"
                    % "/".join(sorted(_LOAD_STATES)),
                    action_index=i, action_type=atype)
        if atype == "wait_for_timeout":
            ms = a.get("ms")
            if not isinstance(ms, int) or ms < 0:
                raise BrowserActionError(
                    "wait_for_timeout 动作的 ms 必须是非负整数",
                    action_index=i, action_type=atype)
        if atype == "goto" and not a.get("url"):
            raise BrowserActionError("goto 动作缺少 url",
                                     action_index=i, action_type=atype)


def _host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _check_host(url, expected_host):
    host = _host_of(url)
    if not expected_host:
        raise BrowserActionError("expected_host 不能为空")
    if host != expected_host.lower():
        raise BrowserActionError(
            "当前页面主机 %s 与 expected_host %s 不一致"
            % (host or "（空）", expected_host))


def _error_detail(action_type, exc):
    """动作异常 -> 可返回给调用方的说明（fill 不回显原始信息）。

    fill 的 Playwright 异常通常包含选择器与超时原因，但也可能与填入值
    同处一条消息；这里只返回安全的分类说明，避免账号值泄漏。
    """
    if action_type != "fill":
        return "：%s" % exc
    low = str(exc).lower()
    if "timeout" in low:
        return "：等待输入框可填写超时（元素未出现、不可见或不可编辑）"
    if ("not visible" in low or "not editable" in low
            or "not attached" in low or "detached" in low
            or "element is not" in low):
        return "：输入框不可见/不可编辑"
    return ""       # 其他未知异常不回显原始信息


def _remaining_ms(deadline):
    return max(0, int((deadline - time.time()) * 1000))


def _action_timeout(action, deadline):
    timeout = action.get("timeout_ms")
    if not isinstance(timeout, int) or timeout <= 0:
        timeout = DEFAULT_ACTION_TIMEOUT_MS
    return max(1, min(timeout, _remaining_ms(deadline)))


def _find_or_open_page(browser, url, timeout_ms, activate_page=True):
    """按 URL（协议+主机+端口）复用已有标签页，没有则新开。

    activate_page=False 时跳过 page.bring_to_front()：该调用在 Windows 上会
    把 Chrome 窗口激活到前台，打断用户当前操作；CDP 动作本身不需要窗口在前台。
    """
    target = urlsplit(url)
    tkey = (target.scheme.lower(), target.netloc.lower())
    exact = same_host = None
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                u = urlsplit(pg.url)
            except ValueError:
                continue
            if u.scheme not in ("http", "https"):
                continue
            if pg.url == url:
                exact = exact or pg
            elif (u.scheme.lower(), u.netloc.lower()) == tkey:
                same_host = same_host or pg
    page = exact or same_host
    if page is None and not activate_page:
        # 后台模式（activate_page=False）：没有同主机标签页时，复用任意
        # http/https 标签页导航，避免 new_page() 让最小化的 Chrome 窗口
        # restore（表现为窗口“闪一下”）。脚本首个动作本就是 goto 登录页。
        for ctx in browser.contexts:
            for pg in ctx.pages:
                try:
                    u = urlsplit(pg.url)
                except ValueError:
                    continue
                if u.scheme in ("http", "https"):
                    page = pg
                    break
            if page is not None:
                break
    if page is None:
        if not browser.contexts:
            raise BrowserActionError(
                "抓包 Chrome 没有可用浏览器上下文", status_code=500)
        page = browser.contexts[0].new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    elif page.url != url:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if activate_page:
        page.bring_to_front()
    return page


def _resolve_fill_value(action, accounts):
    if action.get("value_from"):
        name = str(action["value_from"])
        if name not in accounts:
            raise BrowserActionError("参数名称不存在：%s" % name)
        return accounts[name]
    return "" if action.get("value") is None else str(action["value"])


def run_actions(cdp_port, url, expected_host, actions, timeout_ms, accounts,
                activate_page=True):
    """连接抓包 Chrome 并顺序执行动作；返回结果或抛出 BrowserActionError。

    activate_page=False 时不激活标签页/窗口（避免抢用户前台焦点），并在
    页面创建、每个动作及等待期间把被 Chrome 抢走的前台焦点还给用户。
    """
    url = str(url or "").strip()
    expected_host = str(expected_host or "").strip().lower()
    if not url or not expected_host:
        raise BrowserActionError("url 与 expected_host 不能为空")
    target = urlsplit(url)
    if target.scheme not in ("http", "https") or not target.hostname:
        raise BrowserActionError("url 必须是 http/https 完整地址")
    if target.hostname.lower() != expected_host:
        raise BrowserActionError(
            "url 主机 %s 与 expected_host %s 不一致"
            % (target.hostname, expected_host))
    validate_actions(actions, accounts)
    if not chrome_proc.is_cdp_alive(cdp_port, force=True):
        raise BrowserActionError(
            "CDP 端口 %d 的 Chrome 未启动或不可达（请先启动该实例，"
            "或调用 /api/cdp-profiles/ensure）" % cdp_port)
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        timeout_ms = DEFAULT_TOTAL_TIMEOUT_MS
    timeout_ms = min(timeout_ms, 300000)
    deadline = time.time() + timeout_ms / 1000.0
    results = []
    # 动作开始前记录用户当前前台窗口：activate_page=False 时，页面登录/
    # 跳转仍可能被 Chrome 或页面 JS 激活窗口，用于在各节点把焦点还回去。
    restore_hwnd = _foreground_hwnd() if not activate_page else 0

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise BrowserActionError(
            "主应用缺少 Playwright（请运行 install.bat）：%s" % e,
            status_code=500)

    with _lock_for(cdp_port):
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(
                    "http://127.0.0.1:%d" % cdp_port,
                    timeout=min(timeout_ms, 30000))
                page = _find_or_open_page(
                    browser, url, min(timeout_ms, 30000), activate_page)
                _restore_foreground(restore_hwnd)
                _check_host(page.url, expected_host)

                for i, action in enumerate(actions):
                    # 覆盖上一动作期间（含页面跳转）发生的窗口激活
                    _restore_foreground(restore_hwnd)
                    if _remaining_ms(deadline) <= 0:
                        raise BrowserActionError(
                            "浏览器动作总超时（%dms）" % timeout_ms,
                            action_index=i, action_type=action.get("type"),
                            url=page.url, results=results)
                    atype = action["type"]
                    timeout = _action_timeout(action, deadline)
                    try:
                        if atype == "goto":
                            target = str(action["url"])
                            _check_host(target, expected_host)
                            page.goto(target, wait_until="domcontentloaded",
                                      timeout=timeout)
                            _check_host(page.url, expected_host)
                        elif atype == "reload":
                            # 刷新当前标签页：用于页面空白/“加载失败”后的重试
                            _check_host(page.url, expected_host)
                            page.reload(wait_until="domcontentloaded",
                                        timeout=timeout)
                            _check_host(page.url, expected_host)
                        elif atype == "fill":
                            _check_host(page.url, expected_host)
                            value = _resolve_fill_value(action, accounts)
                            page.fill(str(action["selector"]), value,
                                      timeout=timeout)
                        elif atype == "click":
                            _check_host(page.url, expected_host)
                            page.click(str(action["selector"]),
                                       timeout=timeout)
                        elif atype == "press":
                            _check_host(page.url, expected_host)
                            page.press(str(action["selector"]),
                                       str(action["key"]), timeout=timeout)
                        elif atype == "wait_for_selector":
                            _check_host(page.url, expected_host)
                            selector = str(action["selector"])
                            state = action.get("state") or "visible"
                            end = time.time() + timeout / 1000.0
                            ok = False
                            last_exc = None
                            while time.time() < end:
                                slice_ms = max(
                                    1, min(500,
                                           int((end - time.time()) * 1000)))
                                try:
                                    page.wait_for_selector(
                                        selector, state=state,
                                        timeout=slice_ms)
                                    ok = True
                                    break
                                except PlaywrightError as exc:
                                    last_exc = exc
                                    # 分段等待期间被 Chrome 激活时及时还回焦点
                                    _restore_foreground(restore_hwnd)
                            if ok:
                                results.append({
                                    "type": "wait_for_selector",
                                    "selector": selector,
                                    "state": state,
                                })
                            elif action.get("continue_on_timeout"):
                                results.append({
                                    "type": "wait_for_selector",
                                    "selector": selector,
                                    "state": state,
                                    "timeout": True,
                                })
                            elif last_exc is not None:
                                raise last_exc
                            else:
                                raise BrowserActionError(
                                    "等待元素超时", action_index=i,
                                    action_type=atype, url=page.url,
                                    results=results)
                        elif atype == "wait_for_url":
                            contains = action.get("contains")
                            not_contains = action.get("not_contains")
                            end = time.time() + timeout / 1000.0
                            ok = False
                            while time.time() < end:
                                cur = page.url
                                ok = ((not contains or str(contains) in cur)
                                      and (not not_contains
                                           or str(not_contains) not in cur))
                                if ok:
                                    break
                                page.wait_for_timeout(200)
                                # 长等待期间登录跳转可能激活窗口，及时还回焦点
                                _restore_foreground(restore_hwnd)
                            if not ok:
                                if action.get("continue_on_timeout"):
                                    results.append({
                                        "type": "wait_for_url",
                                        "contains": contains,
                                        "not_contains": not_contains,
                                        "timeout": True,
                                        "url": page.url,
                                    })
                                else:
                                    raise BrowserActionError(
                                        "等待 URL 超时（contains=%s, "
                                        "not_contains=%s）"
                                        % (contains or "", not_contains or ""),
                                        action_index=i, action_type=atype,
                                        url=page.url, results=results)
                            else:
                                results.append({
                                    "type": "wait_for_url",
                                    "url": page.url,
                                })
                        elif atype == "wait_for_load_state":
                            state = action.get("state") or "domcontentloaded"
                            end = time.time() + timeout / 1000.0
                            ok = False
                            last_exc = None
                            while time.time() < end:
                                slice_ms = max(
                                    1, min(500,
                                           int((end - time.time()) * 1000)))
                                try:
                                    page.wait_for_load_state(
                                        state, timeout=slice_ms)
                                    ok = True
                                    break
                                except PlaywrightError as exc:
                                    last_exc = exc
                                    # 分段等待期间被 Chrome 激活时及时还回焦点
                                    _restore_foreground(restore_hwnd)
                            if ok:
                                results.append({
                                    "type": "wait_for_load_state",
                                    "state": state,
                                })
                            elif action.get("continue_on_timeout"):
                                results.append({
                                    "type": "wait_for_load_state",
                                    "state": state,
                                    "timeout": True,
                                })
                            elif last_exc is not None:
                                raise last_exc
                            else:
                                raise BrowserActionError(
                                    "等待页面加载超时", action_index=i,
                                    action_type=atype, url=page.url,
                                    results=results)
                        elif atype == "wait_for_timeout":
                            ms = min(int(action["ms"]),
                                     _remaining_ms(deadline))
                            end = time.time() + ms / 1000.0
                            while time.time() < end:
                                slice_ms = max(
                                    1, min(500,
                                           int((end - time.time()) * 1000)))
                                page.wait_for_timeout(slice_ms)
                                _restore_foreground(restore_hwnd)
                        elif atype == "get_url":
                            results.append({"type": "get_url",
                                            "url": page.url})
                        elif atype == "get_text":
                            selector = action.get("selector") or "body"
                            results.append({
                                "type": "get_text",
                                "selector": selector,
                                "text": page.inner_text(
                                    str(selector), timeout=timeout)[:10000],
                            })
                        elif atype == "get_title":
                            results.append({"type": "get_title",
                                            "title": page.title()})
                        elif atype == "is_visible":
                            results.append({
                                "type": "is_visible",
                                "selector": action["selector"],
                                "visible": page.is_visible(
                                    str(action["selector"])),
                            })
                    except BrowserActionError:
                        raise
                    except PlaywrightError as e:
                        raise BrowserActionError(
                            "动作执行失败（%s）%s"
                            % (atype, _error_detail(atype, e)),
                            action_index=i, action_type=atype,
                            url=page.url, results=results)
                    except Exception as e:
                        raise BrowserActionError(
                            "动作执行异常（%s）：%s" % (atype, e),
                            status_code=500, action_index=i,
                            action_type=atype, url=page.url,
                            results=results)
                return {
                    "ok": True,
                    "url": page.url,
                    "title": page.title(),
                    "results": results,
                }
        except BrowserActionError:
            raise
        except Exception as e:
            raise BrowserActionError(
                "浏览器动作执行失败：%s" % e, status_code=500)
        finally:
            # 异常/超时路径也要把焦点还给用户
            _restore_foreground(restore_hwnd)