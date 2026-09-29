# -*- coding: utf-8 -*-
"""CDP 实时 Cookie 读取 + Authorization 观察缓存。

设计文档：docs/CDP实时读取Cookie查询接口设计/README.md（含完整调研与
实测验证；本文件即其参考实现，按 7.2/7.3 节挂接：webapp 新增
/api/cookies/cdp 路由 + capture.ensure_cdp 注册观察器）。

两部分能力：

1. Cookie（实时）：从抓包 Chrome 的 CDP 调试端口，经浏览器级
   Storage.getCookies 实时读取内存中的 Cookie（含 httpOnly 与 CHIPS
   分区 Cookie），按 CookieStore.query 同口径的主机匹配与分区去重后
   返回，条目格式与现有 /api/cookies/query 对齐。
2. Authorization（观察缓存）：Authorization 不在任何存储里，只能观察
   实际请求头。本模块提供 AuthCache，经 CDPClient 的多 handler 机制
   旁路挂接 Network.requestWillBeSent / requestWillBeSentExtraInfo 事件
   （与 capture 的同名 handler 并行、互不影响），把请求头里的
   Authorization 按精确主机缓存于内存。事件仅在抓包会话期间流动
   （Network.enable 只在抓包时开启），因此缓存在「开抓包 → 操作页面」
   期间积累——与 analysis-guide「先开抓包再操作」工作流天然契合。

依赖：websockets（服务 requirements 已含）。使用与服务 CDPClient 相同的
异步 websockets 库——其客户端默认不发送 Origin 头，无 Chrome 的
--remote-allow-origins 403 问题（详见 README 3.3 节）。
"""

import asyncio
import json
import time
import urllib.request
from collections import OrderedDict
from urllib.parse import urlsplit

import websockets


# ==========================================================================
# 第一部分：Cookie 实时读取
# ==========================================================================

def parse_host(url_or_host):
    """URL 或域名 -> 小写主机名（与 webapp.cookies_query 的解析口径一致）。"""
    s = (url_or_host or "").strip()
    if "://" in s:
        host = urlsplit(s).hostname or ""
    else:
        host = s.split("/")[0].split(":")[0]
    return host.lower()


def _match(cookie_domain, host):
    """浏览器语义域名匹配（与 cookie_store.CookieStore._match 同口径）。

    domain 带前置点（.example.com）-> 主域及所有子域命中；
    host-only（无前置点）-> 仅精确匹配该主机。
    """
    d = (cookie_domain or "").lower()
    if not d or not host:
        return False
    if d.startswith("."):
        return host == d[1:] or host.endswith(d)
    return host == d


def _normalize(c):
    """CDP Cookie -> /api/cookies/query 的 cookies 条目格式。

    见 README 第 4 节字段映射表：expires(-1=会话) 保留为 int；
    partitionKey.topLevelSite -> partitionSite（未分区为空串）；
    size/sameSite/priority 等附加字段不映射，保持与现有响应同构。
    """
    expires = c.get("expires", -1)
    pk = c.get("partitionKey") or {}
    site = str(pk.get("topLevelSite") or "") if isinstance(pk, dict) else ""
    return {
        "name": c.get("name", ""),
        "value": c.get("value", ""),
        "domain": c.get("domain", ""),
        "path": c.get("path", "/"),
        "secure": bool(c.get("secure")),
        "httpOnly": bool(c.get("httpOnly")),
        "expires": int(expires) if isinstance(expires, (int, float)) else -1,
        "partitionSite": site,
    }


def _dedup_partition(cookies, host):
    """同一 (domain, path, name) 的分区变体只留一条（CookieStore 同口径）。

    优先级：分区站点==查询主机 > 未分区 > 其他分区。CDP 的
    partitionKey.topLevelSite 形如 "https://console.example.com"（含协议），
    与主机比较前先取其主机名（与 cookie_store.CookieStore._partition_rank
    同口径，参考实现直接字符串比较会错判为其他分区，落地时修正）。
    """
    best = {}
    for c in cookies:
        k = (c["domain"], c["path"], c["name"])
        site = (c.get("partitionSite") or "").strip().lower()
        if not site:
            rank = 1
        else:
            part_host = urlsplit(site).hostname or site
            rank = 0 if part_host == host else 2
        cur = best.get(k)
        if cur is None or rank < cur[0]:
            best[k] = (rank, c)
    return [v[1] for v in best.values()]


async def fetch_cdp_cookies(cdp_port, timeout=10):
    """短连接读浏览器级 Storage.getCookies，返回 CDP 原始 cookie 列表。

    端点：GET http://127.0.0.1:<port>/json/version 的 webSocketDebuggerUrl
    （浏览器级，不依赖任何页面存在）。抓包 Chrome 未启动/端口不对时抛异常
    （urllib/websockets 各类失败统一上抛，由 query_cdp 转成用户可读文案）。

    /json/version 的 HTTP 请求经 asyncio.to_thread 执行（与
    CDPClient.connect 同款）：urlopen 为同步阻塞调用，端口不可达/
    防火墙丢包时最多阻塞 3 秒，直接跑在事件循环会拖停抓包事件分发与
    其他 HTTP 接口（落地后复查修正）。
    """
    def _get_ws_url():
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % cdp_port,
                timeout=3) as r:
            return json.loads(r.read().decode("utf-8", "replace"))[
                "webSocketDebuggerUrl"]

    ws_url = await asyncio.to_thread(_get_ws_url)
    async with websockets.connect(ws_url, max_size=None, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    resp = json.loads(raw)
    if "error" in resp:
        raise RuntimeError("Storage.getCookies: %s"
                           % resp["error"].get("message"))
    return resp.get("result", {}).get("cookies", [])


async def query_cdp(url_or_host, cdp_port):
    """对外主入口（Cookie 部分）：按主机过滤，返回 (cookies, error)。

    cookies 为 None 时 err 为用户可读的失败原因（用于 HTTP 404 响应）；
    成功时 err 为 None，cookies 为归一化 + 分区去重后的列表，条目格式与
    /api/cookies/query 的 cookies[] 完全一致（cookie_header 由调用方经
    CookieStore.cookie_header 静态方法拼接）。
    """
    host = parse_host(url_or_host)
    if not host:
        return None, "缺少 url 参数或无法解析主机名"
    try:
        all_cookies = await fetch_cdp_cookies(cdp_port)
    except Exception as e:
        return None, ("CDP 读取失败（抓包 Chrome 未启动或调试端口 %d 不可达）: %s"
                      % (cdp_port, e))
    matched = [_normalize(c) for c in all_cookies
               if _match(c.get("domain"), host)]
    if not matched:
        return None, ("没有与主机 %s 匹配的 Cookie（抓包 Chrome 中可能未登录该站点）"
                      % host)
    return _dedup_partition(matched, host), None


# ==========================================================================
# 第二部分：Authorization 观察缓存
# ==========================================================================

def _extract_auth(headers):
    """从请求头 dict 中提取 Authorization 值（头名大小写不敏感）。

    返回字符串或 None。ExtraInfo 的 headers 值偶为数组形态，做拼接兼容。
    """
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "authorization" and v:
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x) for x in v)
            return str(v)
    return None


def _host_of(url):
    """URL -> 小写主机名（非法/非 http(s) 返回空串）。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower()


class AuthCache:
    """从 CDP 网络事件观察到的 Authorization 内存缓存（host 精确匹配）。

    机制：Authorization 是站点 JS/Service Worker 设置的请求头，不存在于
    Cookie 存储，只能观察实际请求。CDPClient 的事件分发循环对同一事件
    会调用所有注册的 handler（app/capture.py _dispatch_loop），因此本类
    的 handler 可与 capture 的同名 handler 旁路并行、互不影响。

    事件流特性（重要）：
    - Network.enable 仅在抓包会话期间对页面会话开启（现有行为），事件
      只在抓包期间流动 → 缓存在「开抓包 → 操作页面」期间积累；
    - 主事件 requestWillBeSent 带完整 URL；ExtraInfo（网络栈实际发送头，
      Service Worker 注入的头可能只出现在这里）只带 requestId 不带 URL，
      且与主事件到达顺序不定——按 (session_id, requestId) 双向暂存合并
      （与 capture.py 处理 req_headers_extra 的思路一致）；
    - 全部访问发生在服务事件循环单线程内（dispatch 任务与 HTTP 路由同
      loop），无需锁。

    安全：值只存内存，不落盘不落日志（日志只记 host 与观察时间）。
    """

    def __init__(self, recent_limit=1024, pending_limit=256):
        self._auths = {}            # host -> {"value": str, "observed_at": float}
        self._recent_host = OrderedDict()  # (sid, requestId) -> host（近期请求）
        self._pending_auth = OrderedDict()  # (sid, requestId) -> value（先到暂存）
        self._recent_limit = recent_limit
        self._pending_limit = pending_limit

    # ---------- CDP 事件 handler（签名与 CDPClient 分发口径一致） ----------

    async def on_request(self, session_id, params):
        """Network.requestWillBeSent：主事件（带 URL）。"""
        req = params.get("request", {}) or {}
        rid = params.get("requestId")
        key = (session_id, rid)
        host = _host_of(req.get("url", ""))
        if not host:
            return
        # 先到的 ExtraInfo 里若有 Authorization，此刻拿到 host 可以落缓存；
        # 双值冲突时 ExtraInfo 值优先（网络栈实际发送头，与"主事件先到、
        # 后到的 ExtraInfo 覆盖"语义一致）
        pending = self._pending_auth.pop(key, None)
        value = pending or _extract_auth(req.get("headers"))
        if value:
            self._auths[host] = {"value": value, "observed_at": time.time()}
        # 记录 (sid,rid)->host，供后到的 ExtraInfo 归属（有界，防泄漏）
        self._recent_host[key] = host
        self._recent_host.move_to_end(key)
        while len(self._recent_host) > self._recent_limit:
            self._recent_host.popitem(last=False)

    async def on_request_extra(self, session_id, params):
        """Network.requestWillBeSentExtraInfo：实际发送头（不带 URL）。"""
        rid = params.get("requestId")
        key = (session_id, rid)
        value = _extract_auth(params.get("headers"))
        if not value:
            return
        host = self._recent_host.get(key)
        if host:
            # 主事件先到：直接按其 host 落缓存
            self._auths[host] = {"value": value, "observed_at": time.time()}
        else:
            # ExtraInfo 先到：暂存，等主事件带 URL 到达时落缓存
            self._pending_auth[key] = value
            self._pending_auth.move_to_end(key)
            while len(self._pending_auth) > self._pending_limit:
                self._pending_auth.popitem(last=False)

    # ---------- 查询 ----------

    def get(self, host):
        """按精确主机匹配（与 CookieStore.query_auth 同口径）。

        返回 {"value": str, "observed_at": float} 或 None。
        Authorization 不跨域携带，只做精确匹配（大小写归一）。
        """
        h = (host or "").strip().lower()
        if not h:
            return None
        entry = self._auths.get(h)
        return dict(entry) if entry else None

    def hosts(self):
        """已观察到的 host 列表（调试用；不含值）。"""
        return sorted(self._auths.keys())

    def clear(self):
        self._auths.clear()
        self._recent_host.clear()
        self._pending_auth.clear()


# 模块级单例：ensure_cdp 挂接与 HTTP 路由查询共用同一实例
auth_cache = AuthCache()
