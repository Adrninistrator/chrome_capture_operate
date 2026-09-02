"""抓包核心：CDP 客户端 + 抓包管理。

- 通过浏览器级 WebSocket + Target.setAutoAttach(flatten) 抓取所有标签页（含抓包期间新打开）。
- 每次开始抓包生成 captured_record/yyyy_MM_dd_HH_mm_ss/ 子目录。
- index.md 汇总：%010d 序号、URL、方法、返回 content-type、请求/返回 body 大小、耗时。
- 每条记录一个 {序号}.md："# request"/"# response" 为重建的 HTTP 原始格式；
  cookie/set-cookie 值与 Authorization 掩码；gzip 由 CDP 解码后即为明文；
  二进制 body 另存文件，正文写 @{文件名}；超大 body 不截断。
- 抓包配置为会话级（不写 conf.json），抓包中调整对后续数据即时生效。
"""
import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlsplit

import websockets

from . import chrome_proc
from .config import CAPTURE_DIR

log = logging.getLogger("app.capture")

STATE_IDLE = "idle"
STATE_CAPTURING = "capturing"
STATE_PAUSED = "paused"

MARK_SUFFIX = "_人工标记"


# ---------- 过滤评估（模块级：抓包中与历史会话 purge 共用同一口径） ----------
def type_filtered(rtype, capture_conf, config):
    """Chrome 资源类型过滤：命中 conf.json type_filter 清单则过滤。

    只过滤纯静态资源类型；Document/XHR/Fetch 等不在默认清单，
    业务接口不会被误杀。
    """
    if not capture_conf.get("type_enabled", True) or not rtype:
        return False
    return rtype in set(config.get("type_filter", []))


def url_filtered(url, capture_conf, config):
    """URL 级过滤：域名选择、URI 忽略规则、后缀。返回 True 表示过滤掉。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    host = (parts.hostname or "").lower()
    if not host:
        return True
    domains = [d.lower() for d in capture_conf.get("domains", [])]
    if domains and host not in domains:
        return True
    path = parts.path or "/"
    uri = path + (("?" + parts.query) if parts.query else "")
    for rule in capture_conf.get("uri_rules", []):
        rtype, rval = rule.get("type"), rule.get("value", "")
        if not rval:
            continue
        if rtype == "prefix" and uri.startswith(rval):
            return True
        if rtype == "equals" and uri == rval:
            return True
        if rtype == "contains" and rval in uri:
            return True
        if rtype == "suffix" and uri.endswith(rval):
            return True
    if capture_conf.get("suffix_enabled", True):
        p = path.lower()
        for suf in config.get("suffix_filter", []):
            if p.endswith(suf.lower()):
                return True
    return False


def evaluate_violation(row, capture_conf, config):
    """记录是否不满足抓包配置（应被删除）。

    评估口径与抓包过滤一致：资源类型 > URL（域名/URI 规则/后缀）>
    content-type。抓包中的 purge 与历史会话的 purge 共用。
    """
    if type_filtered(row.get("type", ""), capture_conf, config):
        return True
    if url_filtered(row.get("url", ""), capture_conf, config):
        return True
    ct = (row.get("content_type") or "").lower()
    if capture_conf.get("content_type_enabled", True) and ct:
        for item in config.get("content_type_filter", []):
            if item.lower() in ct:
                return True
    return False


def rename_with_retry(src, dst, attempts=5, delay=0.4):
    """重命名（带重试）：Windows 下杀毒软件/索引器会短暂锁定刚写入的文件，
    导致 os.rename 报 WinError 5 拒绝访问。短暂重试可覆盖该瞬态窗口。
    返回 (ok, error)。
    """
    err = None
    for i in range(attempts):
        try:
            os.rename(src, dst)
            return True, None
        except OSError as e:
            err = e
            if i < attempts - 1:
                time.sleep(delay)
    return False, err


def mask_header_value(name, value):
    """cookie/set-cookie 仅保留 key、值掩码 ***；authorization 整体掩码。"""
    n = name.lower()
    if n == "authorization":
        return "***"
    if n in ("cookie", "set-cookie"):
        parts = []
        for i, seg in enumerate(str(value).split(";")):
            seg = seg.strip()
            if "=" in seg:
                k = seg.split("=", 1)[0].strip()
                # set-cookie 的属性（Expires/Path/Max-Age 等）保留，只掩码 cookie 本体值
                if n == "set-cookie" and i > 0:
                    parts.append(seg)
                else:
                    parts.append("%s=***" % k)
            else:
                parts.append(seg)
        return "; ".join(parts)
    return value


def mask_headers(headers):
    return {k: mask_header_value(k, v) for k, v in (headers or {}).items()}


class CDPClient:
    """浏览器级 CDP WebSocket 客户端（flatten session + auto-attach）。

    接收循环只读帧：响应按 id 结算、事件进队列；独立的 dispatch 任务串行
    处理事件。事件处理器内可安全发起 CDP 命令往返（getResponseBody 等）——
    若在处理器的 await 中直接等响应，响应只能由接收循环读取而它被堵住，
    会形成死锁直到超时（曾导致每条记录延迟 30s、body 全丢、保活断连）。
    """

    def __init__(self, port):
        self.port = port
        self.ws = None
        self._id = 0
        self._pending = {}
        self._handlers = {}
        self._recv_task = None
        self._dispatch_task = None
        self._event_queue = asyncio.Queue()
        self.on_close = None  # 连接断开回调（无参，async）

    async def connect(self):
        def _get_ws_url():
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/version" % self.port,
                    timeout=3) as r:
                return json.loads(r.read().decode("utf-8", "replace"))[
                    "webSocketDebuggerUrl"]

        ws_url = await asyncio.to_thread(_get_ws_url)
        self.ws = await websockets.connect(ws_url, max_size=None,
                                           ping_interval=20)
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        # 注意：禁用 Target.setAutoAttach——在 target 创建瞬间 attach 会与
        # window.open 的同步开窗路径死锁（itsm 复制按钮卡死事故，渲染主线程
        # 阻塞约 30s）。改为手动 attach：仅在 target 导航出真实 http(s) URL
        # 后（targetInfoChanged）才 attach。
        await self.cmd("Target.setDiscoverTargets", {"discover": True})

    def on(self, method, cb):
        self._handlers.setdefault(method, []).append(cb)

    async def cmd(self, method, params=None, session_id=None, timeout=20):
        if not self.ws:
            raise RuntimeError("CDP 未连接")
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self.ws.send(json.dumps(msg))
        res = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in res:
            raise RuntimeError("%s: %s" % (method, res["error"].get("message")))
        return res.get("result", {})

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    self._event_queue.put_nowait(msg)
        except Exception as e:
            log.info("CDP 连接断开: %s", e)
        finally:
            self.ws = None
            self._event_queue.put_nowait(None)  # 通知 dispatch 退出
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("CDP 连接断开"))
            self._pending.clear()

    async def _dispatch_loop(self):
        """串行处理事件（保持 CDP 事件顺序）；队列排空后再触发 on_close。"""
        while True:
            msg = await self._event_queue.get()
            if msg is None:
                break
            for cb in self._handlers.get(msg.get("method"), []):
                try:
                    await cb(msg.get("sessionId"), msg.get("params", {}))
                except Exception:
                    log.exception("CDP 事件处理异常 %s", msg.get("method"))
        if self.on_close:
            try:
                await self.on_close()
            except Exception:
                log.exception("CDP on_close 处理异常")

    async def close(self):
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


class CaptureManager:
    """抓包状态机与记录落盘。"""

    def __init__(self, config):
        self.config = config
        self.state = STATE_IDLE
        self.cdp = None
        self.session_name = None
        self.session_dir = None
        self.counter = 0
        self.pending = {}        # (session_id, request_id) -> dict
        self._extra_req = {}     # key -> 请求原始头（先于主事件到达的暂存）
        self._extra_resp = {}    # key -> 响应原始头
        self._attached = set()   # 已手动 attach 的 targetId
        self.seen_hosts = {}     # host -> first_seen_ts（CDP 连接后持续收集）
        self.records = []        # 当前会话已落盘的记录行（row dict），序号不变
        self.marked = False      # 当前会话是否被人工标记（结束后目录加后缀）
        self.on_event = None     # async broadcast(dict)，由 webapp 注入
        # 会话级抓包配置（不写 conf.json）
        self.capture_conf = {
            "suffix_enabled": True,
            "content_type_enabled": True,
            "type_enabled": True,  # 根据 Chrome 资源类型过滤（清单在 conf.json）
            "domains": [],        # 空 = 全部
            "uri_rules": [],      # [{type: prefix|equals|contains|suffix, value}]
            "ws_capture": False,  # 暂不实现
        }
        self._index_lock = asyncio.Lock()

    # ---------- 状态 ----------
    def status(self):
        cdp_port = self.config.get("cdp_port")
        return {
            "state": self.state,
            "session": self.session_name,
            "cdp_connected": self.cdp is not None and self.cdp.ws is not None,
            "chrome_alive": chrome_proc.is_cdp_alive(cdp_port),
            "record_count": self.counter,
            "marked": self.marked,
        }

    async def _broadcast(self, payload):
        if self.on_event:
            try:
                await self.on_event(payload)
            except Exception:
                log.exception("广播失败")

    # ---------- CDP 连接 ----------
    async def ensure_cdp(self):
        """确保 CDP 已连接（用于网址收集与抓包）。"""
        if self.cdp and self.cdp.ws:
            return True
        cdp_port = self.config.get("cdp_port")
        if not chrome_proc.is_cdp_alive(cdp_port):
            return False
        self.cdp = CDPClient(cdp_port)
        self.cdp.on("Target.targetCreated", self._on_target_created)
        self.cdp.on("Target.targetInfoChanged", self._on_target_info_changed)
        self.cdp.on("Target.targetDestroyed", self._on_target_destroyed)
        self.cdp.on("Network.requestWillBeSent", self._on_request)
        self.cdp.on("Network.responseReceived", self._on_response)
        self.cdp.on("Network.loadingFinished", self._on_finished)
        self.cdp.on("Network.loadingFailed", self._on_failed)
        # 敏感/原始头（Cookie、Set-Cookie、h2伪头、部分 h3 响应头）只走
        # ExtraInfo 事件，主事件的 headers 不含它们
        self.cdp.on("Network.requestWillBeSentExtraInfo",
                    self._on_request_extra)
        self.cdp.on("Network.responseReceivedExtraInfo",
                    self._on_response_extra)
        self.cdp.on_close = self._on_cdp_close

        await self.cdp.connect()
        log.info("CDP 已连接，端口 %d", cdp_port)
        # 连接成功即推送状态（chrome_alive 变 true），前端无需等轮询兜底
        await self._broadcast({"type": "state",
                               **await asyncio.to_thread(self.status)})
        return True

    async def _on_cdp_close(self):
        was = self.state
        self.state = STATE_IDLE
        self._attached.clear()
        # 连接断开时在途请求同样落盘，避免记录丢失
        pending = list(self.pending.values())
        self.pending.clear()
        self._extra_req.clear()
        self._extra_resp.clear()
        if was in (STATE_CAPTURING, STATE_PAUSED):
            for rec in pending:
                if not rec.get("response"):
                    rec["error"] = "Chrome 连接断开时响应未完成"
                await self._write_record(rec, None, None)
            mark_err = self._apply_mark_suffix()
            log.info("Chrome 调试端口断开，抓包已结束: %s", self.session_name)
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status),
                               "message": "Chrome 进程已结束，抓包已停止"})
        if mark_err:
            await self._broadcast({
                "type": "mark_failed",
                "message": "标记重命名失败（%s），目录保持原名: %s" % (
                    mark_err, self.session_name)})

    # ---------- target 生命周期（手动 attach 模式） ----------
    def _record_target_host(self, tinfo):
        """网址收集（任何状态下）：从 page target 的 URL 提取 host。"""
        if tinfo.get("type") != "page":
            return
        url = tinfo.get("url", "")
        try:
            parts = urlsplit(url)
        except ValueError:
            return
        if parts.scheme.lower() not in ("http", "https"):
            return
        host = (parts.hostname or "").lower()
        if host and host not in self.seen_hosts:
            self.seen_hosts[host] = time.time()
            asyncio.create_task(self._broadcast(
                {"type": "hosts", "hosts": sorted(self.seen_hosts)}))

    async def _maybe_attach(self, tinfo):
        """抓包中且 target 已有真实 http(s) URL 时才 attach + Network.enable。

        绝不在创建瞬间（about:blank）attach：window.open 的同步开窗路径
        会与新 target 的调试初始化互相等待，渲染主线程阻塞约 30s。
        """
        if self.state != STATE_CAPTURING:
            return
        if tinfo.get("type") not in ("page", "iframe"):
            return
        url = (tinfo.get("url") or "").lower()
        if not url.startswith(("http://", "https://")):
            return
        tid = tinfo.get("targetId")
        if not tid or tid in self._attached:
            return
        self._attached.add(tid)
        try:
            r = await self.cdp.cmd("Target.attachToTarget",
                                   {"targetId": tid, "flatten": True})
            await self.cdp.cmd("Network.enable",
                               session_id=r.get("sessionId"))
            log.info("已 attach: %s %s", tid, tinfo.get("url", "")[:60])
        except Exception as e:
            self._attached.discard(tid)
            log.warning("attach 失败 %s: %s", tid, e)

    async def _on_target_created(self, _sid, params):
        tinfo = params.get("targetInfo", {})
        self._record_target_host(tinfo)
        # 创建瞬间不 attach（见 _maybe_attach 注释）；iframe 创建即带真实
        # URL，可立即 attach
        if tinfo.get("type") == "iframe":
            await self._maybe_attach(tinfo)

    async def _on_target_info_changed(self, _sid, params):
        tinfo = params.get("targetInfo", {})
        self._record_target_host(tinfo)
        await self._maybe_attach(tinfo)

    async def _on_target_destroyed(self, _sid, params):
        self._attached.discard(params.get("targetId"))

    # ---------- 抓包控制 ----------
    async def start(self):
        if self.state == STATE_CAPTURING:
            return False, "已在抓包中"
        if self.state == STATE_PAUSED:
            return False, "抓包处于暂停中，请继续抓包或结束抓包"
        cdp_port = self.config.get("cdp_port")
        if not chrome_proc.is_cdp_alive(cdp_port):
            return False, "监听调试端口的 Chrome 未启动，请先启动用于抓包的 Chrome 进程"
        if not await self.ensure_cdp():
            return False, "CDP 连接失败，请确认 Chrome 已以调试端口启动"
        self.session_name = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        self.session_dir = os.path.join(CAPTURE_DIR, self.session_name)
        os.makedirs(self.session_dir, exist_ok=True)
        self.counter = 0
        self.pending.clear()
        self._extra_req.clear()
        self._extra_resp.clear()
        self.records.clear()
        self.marked = False       # 新会话重置标记
        self.state = STATE_CAPTURING
        # attach 所有已存在且有真实 URL 的页面（抓包期间新开的页面由
        # targetInfoChanged 触发 _maybe_attach）
        await self._attach_existing()
        with open(os.path.join(self.session_dir, "index.md"), "w",
                  encoding="utf-8") as f:
            f.write(self._index_header())
        log.info("开始抓包: %s", self.session_name)
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        return True, "抓包已开始: %s" % self.session_name

    async def _attach_existing(self):
        try:
            r = await self.cdp.cmd("Target.getTargets")
            for t in r.get("targetInfos", []):
                await self._maybe_attach(t)
        except Exception as e:
            log.warning("枚举 target 失败: %s", e)

    async def pause(self):
        """暂停抓包：不清空当前显示的记录，之后可以继续。"""
        if self.state != STATE_CAPTURING:
            return False, "当前未在抓包，无法暂停"
        self.state = STATE_PAUSED
        log.info("暂停抓包: %s", self.session_name)
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        return True, "抓包已暂停（记录保留，可继续）"

    async def resume(self):
        """继续抓包：从暂停恢复，沿用当前会话目录与序号。"""
        if self.state != STATE_PAUSED:
            return False, "当前未处于暂停状态"
        self.state = STATE_CAPTURING
        log.info("继续抓包: %s", self.session_name)
        # 暂停期间新打开的页面可能未 attach，恢复时补挂
        await self._attach_existing()
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        return True, "抓包已继续: %s" % self.session_name

    async def mark(self):
        """标记当前抓包会话：本次结束后目录名增加 _人工标记 后缀。"""
        if self.state not in (STATE_CAPTURING, STATE_PAUSED):
            return False, "当前未在抓包，无法标记"
        if self.marked:
            return False, "当前会话已标记"
        self.marked = True
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        return True, "已标记：本次抓包结束后，目录名将增加后缀" + MARK_SUFFIX

    def _apply_mark_suffix(self):
        """抓包结束后：被标记的会话目录重命名加后缀。"""
        if not self.marked or not self.session_dir:
            return
        if self.session_dir.endswith(MARK_SUFFIX):
            return
        new_dir = self.session_dir + MARK_SUFFIX
        ok, err = rename_with_retry(self.session_dir, new_dir)
        if ok:
            self.session_dir = new_dir
            self.session_name = os.path.basename(new_dir)
            log.info("会话目录已标记: %s", self.session_name)
        else:
            log.warning("标记重命名失败: %s", err)
            return err

    async def stop(self, reason="人工停止"):
        if self.state not in (STATE_CAPTURING, STATE_PAUSED):
            return False, "当前未在抓包"
        self.state = STATE_IDLE
        # 停止时仍在途的请求按已有数据落盘，避免记录丢失
        pending = list(self.pending.values())
        self.pending.clear()
        self._extra_req.clear()
        self._extra_resp.clear()
        for rec in pending:
            if not rec.get("response"):
                rec["error"] = "抓包停止时响应未完成"
            await self._write_record(rec, None, None)
        # 抓包中逐条追加的 index 行序为响应完成顺序（与序号=请求发起顺序
        # 在并发时不一致），结束时按序号升序重写一次
        await asyncio.to_thread(self._rewrite_index)
        mark_err = self._apply_mark_suffix()
        log.info("停止抓包(%s): %s", reason, self.session_name)
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        if mark_err:
            await self._broadcast({
                "type": "mark_failed",
                "message": "标记重命名失败（%s），目录保持原名: %s" % (
                    mark_err, self.session_name)})
        return True, "抓包已停止: %s" % self.session_name

    def _index_header(self):
        return ("# 抓包汇总 %s\n\n" % self.session_name
                + "| 序号 | 请求时间 | 方法 | 资源类型 | URL | 返回content-type | 请求body字节数 | "
                  "返回body字节数 | 耗时(ms) |\n"
                + "|---|---|---|---|---|---|---|---|---|\n")

    # ---------- 过滤 ----------
    def _filtered_by_type(self, rtype):
        return type_filtered(rtype, self.capture_conf, self.config)

    def _filtered_by_url(self, url):
        return url_filtered(url, self.capture_conf, self.config)

    def _filtered_by_content_type(self, headers):
        if not self.capture_conf.get("content_type_enabled", True):
            return False
        ct = ""
        for k, v in (headers or {}).items():
            if k.lower() == "content-type":
                ct = v.lower()
                break
        if not ct:
            return False
        for item in self.config.get("content_type_filter", []):
            if item.lower() in ct:
                return True
        return False

    # ---------- 记录组装 ----------
    async def _on_request(self, session_id, params):
        req = params.get("request", {})
        url = req.get("url", "")
        try:
            parts = urlsplit(url)
        except ValueError:
            return
        # 忽略 chrome://、chrome-extension://、devtools://、data: 等非 HTTP
        # 流量（favicon2、new-tab-page、resources 等内部网址即来源于此）
        if parts.scheme.lower() not in ("http", "https"):
            return
        host = (parts.hostname or "").lower()
        if host and host not in self.seen_hosts:
            self.seen_hosts[host] = time.time()
            await self._broadcast({"type": "hosts",
                                   "hosts": sorted(self.seen_hosts)})
        if self.state != STATE_CAPTURING:
            return

        key = (session_id, params.get("requestId"))
        # 重定向：同一 requestId 链上收到新的 requestWillBeSent，先终结上一条
        if "redirectResponse" in params and key in self.pending:
            prev = self.pending.pop(key)
            prev["response"] = params["redirectResponse"]
            await self._write_record(prev, None, None)

        if self._filtered_by_type(params.get("type", "")):
            self._extra_req.pop(key, None)
            self._extra_resp.pop(key, None)
            return
        if self._filtered_by_url(url):
            self._extra_req.pop(key, None)
            self._extra_resp.pop(key, None)
            return
        self.counter += 1
        rec = {
            "seq": self.counter,
            "request": req,
            "timestamp": params.get("timestamp", time.time()),
            "wall_time": params.get("wallTime"),  # epoch 秒，用于展示请求时间
            "type": params.get("type", ""),       # Chrome 资源类型
            "response": None,
            "session_id": session_id,
            "request_id": params.get("requestId"),
            "post_data": req.get("postData"),
            # ExtraInfo 可能先于主事件到达：挂载暂存的原始头
            "req_headers_extra": self._extra_req.pop(key, None),
            "resp_headers_extra": None,
        }
        self.pending[key] = rec
        # postData 未内联时（较大 body）立即补取——请求完成后 CDP 侧就取不到了
        if rec["post_data"] is None and req.get("hasPostData"):
            rec["post_data"] = await self._fetch_post_data(rec)

    async def _on_request_extra(self, session_id, params):
        if self.state != STATE_CAPTURING:
            return
        key = (session_id, params.get("requestId"))
        rec = self.pending.get(key)
        if rec:
            rec["req_headers_extra"] = params.get("headers", {})
        else:
            self._extra_req[key] = params.get("headers", {})

    async def _on_response_extra(self, session_id, params):
        if self.state != STATE_CAPTURING:
            return
        key = (session_id, params.get("requestId"))
        rec = self.pending.get(key)
        if rec:
            rec["resp_headers_extra"] = params.get("headers", {})
        else:
            self._extra_resp[key] = params.get("headers", {})

    async def _fetch_post_data(self, rec):
        if not self.cdp or not self.cdp.ws:
            return None
        try:
            r = await self.cdp.cmd("Network.getRequestPostData",
                                   {"requestId": rec["request_id"]},
                                   session_id=rec["session_id"])
            return r.get("postData")
        except Exception as e:
            log.info("取请求体失败 seq=%s: %s", rec["seq"], e)
            return None

    async def _on_response(self, session_id, params):
        if self.state != STATE_CAPTURING:
            return
        key = (session_id, params.get("requestId"))
        rec = self.pending.get(key)
        if not rec:
            return
        resp = params.get("response", {})
        # content-type 过滤用合并后的头判断（ExtraInfo 可能含更全的头）
        headers = rec.get("resp_headers_extra") or resp.get("headers")
        if self._filtered_by_content_type(headers):
            self.pending.pop(key, None)
            self._extra_req.pop(key, None)
            self._extra_resp.pop(key, None)
            return
        rec["response"] = resp

    async def _on_finished(self, session_id, params):
        if self.state != STATE_CAPTURING:
            return
        key = (session_id, params.get("requestId"))
        rec = self.pending.pop(key, None)
        self._extra_req.pop(key, None)
        self._extra_resp.pop(key, None)
        if not rec:
            return
        body, b64 = None, False
        try:
            res = await self.cdp.cmd(
                "Network.getResponseBody",
                {"requestId": params.get("requestId")},
                session_id=session_id, timeout=30)
            body, b64 = res.get("body", ""), res.get("base64Encoded", False)
        except Exception as e:
            log.info("取响应体失败 seq=%s: %s", rec["seq"], e)
        await self._write_record(rec, (body, b64),
                                 params.get("timestamp"))

    async def _on_failed(self, session_id, params):
        if self.state != STATE_CAPTURING:
            return
        key = (session_id, params.get("requestId"))
        rec = self.pending.pop(key, None)
        self._extra_req.pop(key, None)
        self._extra_resp.pop(key, None)
        if rec:
            rec["error"] = params.get("errorText", "loadingFailed")
            await self._write_record(rec, None, params.get("timestamp"))

    # ---------- 落盘 ----------
    async def _write_record(self, rec, body_info, finish_ts):
        seq = rec["seq"]
        req, resp = rec["request"], rec.get("response")
        seq_str = "%010d" % seq
        lines = []
        time_str, datetime_str = self._fmt_wall(rec.get("wall_time"))

        # --- request ---
        url = req.get("url", "")
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        # 原始头优先用 ExtraInfo（含 Cookie/h2 伪头），缺失退回主事件
        req_headers = rec.get("req_headers_extra") or req.get("headers")
        lines.append("# request")
        req_meta_idx = len(lines)
        lines.append("")  # 占位：[请求时间/body字节数]，body 处理后回填
        lines.append("%s %s HTTP/1.1" % (req.get("method", "GET"), path))
        for k, v in mask_headers(req_headers).items():
            lines.append("%s: %s" % (k, v))
        lines.append("")
        req_body_bytes = b""
        post = rec.get("post_data")
        if post is not None:
            req_body_bytes = post.encode("utf-8", "replace")
            lines.append(post)
        elif req.get("hasPostData"):
            lines.append("<请求包含 body 但获取失败>")
        meta = "[请求时间: %s]" % (datetime_str or "-")
        if rec.get("type"):
            meta += " [资源类型: %s]" % rec["type"]
        meta += " [请求body字节数: %d]" % len(req_body_bytes)
        lines[req_meta_idx] = meta
        lines.append("")

        # --- response ---
        lines.append("# response")
        resp_meta_idx = len(lines)
        lines.append("")  # 占位：[返回body字节数]
        resp_body_bytes = b""
        # 原始头优先用 ExtraInfo（含 Set-Cookie、部分 h3 响应头）
        resp_headers = (rec.get("resp_headers_extra")
                        or (resp.get("headers") if resp else None))
        if resp:
            lines.append("HTTP/1.1 %s %s" % (resp.get("status", ""),
                                             resp.get("statusText", "")))
            for k, v in mask_headers(resp_headers).items():
                lines.append("%s: %s" % (k, v))
            lines.append("")
            if body_info and body_info[0]:
                body, b64 = body_info
                if b64:
                    resp_body_bytes = base64.b64decode(body)
                    ext = self._ext_of(resp.get("mimeType"))
                    fname = "%s%s" % (seq_str, ext)
                    await asyncio.to_thread(
                        self._write_bytes, fname, resp_body_bytes)
                    lines.append("@{%s}" % fname)
                else:
                    resp_body_bytes = body.encode("utf-8", "replace")
                    lines.append(body)  # gzip 已被 CDP 解码，此处即明文
        elif rec.get("error"):
            lines.append("<请求失败: %s>" % rec["error"])
        else:
            lines.append("<无响应数据>")
        lines[resp_meta_idx] = "[返回body字节数: %d]" % len(resp_body_bytes)

        await asyncio.to_thread(self._write_bytes, seq_str + ".md",
                                "\n".join(lines).encode("utf-8", "replace"))

        duration_ms = 0
        if finish_ts and rec.get("timestamp"):
            duration_ms = int((finish_ts - rec["timestamp"]) * 1000)
        row = {
            "seq": seq_str,
            "time": time_str,
            "method": req.get("method", ""),
            "type": rec.get("type") or "",
            "url": url,
            "content_type": self._header_of(resp_headers, "content-type"),
            "req_size": len(req_body_bytes),
            "resp_size": len(resp_body_bytes),
            "duration_ms": duration_ms,
        }
        self.records.append(row)
        async with self._index_lock:
            await asyncio.to_thread(self._append_index, row)
        await self._broadcast({"type": "record", "record": row})

    # ---------- 删除不满足条件的记录 ----------
    def record_violates(self, row):
        """记录是否不满足当前抓包配置（应被删除）。评估口径与抓包过滤一致。"""
        return evaluate_violation(row, self.capture_conf, self.config)

    async def purge(self):
        """删除不满足当前抓包配置的记录：页面显示与保存文件同步删除，序号不变。"""
        if not self.session_dir:
            return False, "当前没有抓包会话"
        victims = [r for r in self.records if self.record_violates(r)]
        keep = [r for r in self.records if not self.record_violates(r)]
        # 删除记录文件（.md 及同名二进制文件）
        for r in victims:
            seq = r["seq"]
            try:
                for f in os.listdir(self.session_dir):
                    if f.startswith(seq + "."):
                        try:
                            os.remove(os.path.join(self.session_dir, f))
                        except OSError as e:
                            log.warning("删除文件失败 %s: %s", f, e)
            except OSError:
                pass
        self.records = keep
        # 不满足条件的在途请求一并丢弃（不产生文件）
        for key, rec in list(self.pending.items()):
            pseudo = {"url": rec["request"].get("url", ""),
                      "type": rec.get("type", ""), "content_type": ""}
            if self.record_violates(pseudo):
                self.pending.pop(key, None)
        await asyncio.to_thread(self._rewrite_index)
        await self._broadcast({
            "type": "purged",
            "deleted": len(victims),
            "deleted_seqs": [r["seq"] for r in victims],
            "record_count": len(self.records),
        })
        log.info("删除不满足条件的记录: 删 %d 留 %d", len(victims),
                 len(self.records))
        return True, "已删除 %d 条不满足条件的记录，保留 %d 条" % (
            len(victims), len(self.records))

    def _rewrite_index(self):
        """全量重写 index.md，行按序号升序排。

        抓包中逐条 append 的行序是"响应完成顺序"（序号是请求发起顺序，
        并发请求时两者不同）；停止抓包与 purge 时经此重写为按序号排序，
        与文件名序一致、符合阅读直觉。"""
        lines = [self._index_header()]
        for row in sorted(self.records, key=lambda r: r["seq"]):
            lines.append(self._index_row(row))
        with open(os.path.join(self.session_dir, "index.md"), "w",
                  encoding="utf-8") as f:
            f.writelines(lines)

    @staticmethod
    def _index_row(row):
        return ("| %s | %s | %s | %s | %s | %s | %d | %d | %d |\n" % (
            row["seq"], row["time"] or "-", row["method"],
            row["type"] or "-", row["url"], row["content_type"],
            row["req_size"], row["resp_size"], row["duration_ms"]))

    def _write_bytes(self, name, data):
        with open(os.path.join(self.session_dir, name), "wb") as f:
            f.write(data)

    def _append_index(self, row):
        with open(os.path.join(self.session_dir, "index.md"), "a",
                  encoding="utf-8") as f:
            f.write(self._index_row(row))

    @staticmethod
    def _fmt_wall(wall):
        """wallTime(epoch秒) -> (HH:MM:SS.mmm, yyyy-MM-dd HH:MM:SS.mmm)。"""
        if not wall:
            return "", ""
        ms = min(999, int(round((wall % 1) * 1000)))
        t = time.localtime(wall)
        return (time.strftime("%H:%M:%S", t) + ".%03d" % ms,
                time.strftime("%Y-%m-%d %H:%M:%S", t) + ".%03d" % ms)

    @staticmethod
    def _header_of(headers, name):
        for k, v in (headers or {}).items():
            if k.lower() == name:
                return v.split(";")[0].strip()
        return ""

    @staticmethod
    def _ext_of(mime):
        ext = mimetypes.guess_extension((mime or "").split(";")[0].strip())
        return ext or ".bin"
