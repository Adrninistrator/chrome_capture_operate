"""Cookie 存储：仅内存、不落盘。

- 接收 Chrome 插件的全量推送（整体替换）。
- 查询按浏览器语义：返回"浏览器访问某主机时真实会带的所有 cookie"，
  即 cookie 的 domain 为查询主机的后缀匹配（.example.com 匹配 www.example.com）；
  host-only cookie（domain 无前置点）仅精确匹配。
- 支持 Partitioned 分区 cookie：chrome.cookies API 默认只作用于未分区
  cookie，插件按 partitionKey.topLevelSite 补读后推送；同一
  (domain, path, name) 可能有多个分区变体，查询时按
  "分区站点=查询主机 > 未分区 > 其他分区" 取一条（与浏览器随请求
  发送的行为一致：只有与请求顶级站点一致的分区才会随请求携带）。
- 记录最近 50 次接收情况（时间/原因/地址/数量/成功失败），与插件推送记录字段一致。
"""
import threading
import time
from collections import deque
from urllib.parse import urlsplit


class CookieStore:
    def __init__(self):
        self._lock = threading.Lock()
        # Chrome profile（多开实例）编号 -> {(domain,path,name,partition): dict}
        # 编号 0 = 未设置编号的插件（默认实例），兼容未配置多开的推送
        self._stores = {0: {}}
        self._receives = deque(maxlen=50)
        self.last_push_time = None  # 最近一次成功推送时间戳

    def _store(self, profile_id):
        """取（或惰性建）指定编号的 cookie 存储。"""
        pid = int(profile_id or 0)
        if pid not in self._stores:
            self._stores[pid] = {}
        return self._stores[pid]

    # ---------- 接收 ----------
    @staticmethod
    def _partition_site(c):
        """分区键的顶级站点（如 https://signin.volcengine.com），未分区为空串。"""
        pk = c.get("partitionKey")
        if isinstance(pk, dict):
            return str(pk.get("topLevelSite") or "")
        return str(pk or "")

    def receive(self, cookies, reason, source_addr, profile_id=None):
        """接收插件推送（整体替换该编号实例的快照）。

        profile_id 为插件设置的"本实例编号"（多开区分），未设置为 0。
        """
        ok, count = True, 0
        try:
            if not isinstance(cookies, list):
                raise ValueError("cookies 必须是数组")
            new = {}
            for c in cookies:
                name = c.get("name")
                domain = c.get("domain", "")
                if not name or not domain:
                    continue
                path = c.get("path", "/")
                site = self._partition_site(c)
                key = (domain, path, name, site)
                new[key] = {
                    "name": name,
                    "value": c.get("value", ""),
                    "domain": domain,
                    "path": path,
                    "secure": bool(c.get("secure")),
                    "httpOnly": bool(c.get("httpOnly")),
                    "expires": c.get("expires") or c.get("expirationDate"),
                    "partitionSite": site,
                }
            count = len(new)
            with self._lock:
                self._store(profile_id if profile_id is not None else 0)
                self._stores[int(profile_id or 0)] = new
                self.last_push_time = time.time()
        except Exception:
            ok = False
        with self._lock:
            self._receives.appendleft({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason or "未知",
                "address": source_addr or "",
                "count": count,
                "profile": int(profile_id or 0),
                # 域名去点（.qq.com -> qq.com）后去重排序（与插件推送记录
                # domains 口径一致）。注意：元素必须是自包含表达式——若把
                # str() 包在整个生成器外层（str(<genexp>)），返回的是生成器
                # 对象的字符串形式，sorted() 会把它按字符排序（曾因此把
                # "<generator object ... at 0x...>" 拆成单字符列表渲染）。
                "domains": sorted(
                    {str(c.get("domain", ""))[1:]
                     if str(c.get("domain", "")).startswith(".")
                     else str(c.get("domain", ""))
                     for c in (cookies or [])
                     if isinstance(c, dict) and c.get("domain")}),
                # cookie name 去重排序（多条 cookie 常有重名 key）
                "keys": sorted(
                    {str(c.get("name", ""))
                     for c in (cookies or [])
                     if isinstance(c, dict) and c.get("name")}),
                "success": ok,
            })
        return ok, count

    def receives(self):
        with self._lock:
            return list(self._receives)

    def total(self):
        with self._lock:
            return sum(len(s) for s in self._stores.values())

    # ---------- 查询 ----------
    def query(self, url_or_host, profile_id=None):
        """按浏览器语义返回访问该主机时会携带的 cookie 列表。

        profile_id 为空/None 时在所有 Chrome profile 中查找，查到即返回；
        非空时仅在该编号实例中查找。
        返回 (cookies_list, error)。error 为 None 表示成功。
        """
        if not url_or_host:
            return None, "缺少 url 参数"
        host = url_or_host.strip()
        if "://" in host:
            host = urlsplit(host).hostname or ""
        else:
            host = host.split("/")[0].split(":")[0]
        host = host.lower()
        if not host:
            return None, "无法从 %r 解析出主机名" % url_or_host

        pid = int(profile_id) if profile_id not in (None, "", 0) else None
        with self._lock:
            has_any = any(self._stores.values())
            if pid is not None:
                stores = [self._stores.get(pid, {})]
            else:
                stores = list(self._stores.values())
            all_cookies = [c for s in stores for c in s.values()]
        if not has_any:
            return None, ("当前没有任何 Cookie：请确认 Chrome 插件已安装、"
                          "推送目标地址配置正确且已成功推送过")

        matched = [c for c in all_cookies if self._match(c["domain"], host)]
        if not matched:
            if pid is not None:
                return None, ("没有与主机 %s 匹配的 Cookie（Chrome profile %d "
                              "未推送该网站的 Cookie，或插件未选择该编号）"
                              % (host, pid))
            return None, "没有与主机 %s 匹配的 Cookie（插件可能尚未推送该网站的 Cookie）" % host
        return self._pick_variants(matched, host), None

    @staticmethod
    def _variant_rank(c, host):
        """同名 cookie 多分区变体的取舍顺序（越小越优先）。

        浏览器随请求发送的分区 cookie 是"分区站点=请求顶级站点"的那份，
        故分区站点与查询主机一致者优先；其次未分区；最后其他分区
        （如 SSO 站点分区下的登录态，兜底可用）。
        """
        site = (c.get("partitionSite") or "").strip().lower()
        if not site:
            return 1  # 未分区
        part_host = urlsplit(site).hostname or site
        return 0 if part_host == host else 2

    @classmethod
    def _pick_variants(cls, matched, host):
        """同一 (domain, path, name) 的多个分区变体只保留一条，按上述优先级。"""
        best, order = {}, []
        for c in matched:
            k = (c["domain"], c["path"], c["name"])
            if k not in best:
                order.append(k)
                best[k] = c
            elif cls._variant_rank(c, host) < cls._variant_rank(best[k], host):
                best[k] = c
        return [best[k] for k in order]

    @staticmethod
    def _match(cookie_domain, host):
        d = (cookie_domain or "").lower()
        if d.startswith("."):
            bare = d[1:]
            return host == bare or host.endswith("." + bare)
        return host == d

    @staticmethod
    def cookie_header(cookies):
        return "; ".join("%s=%s" % (c["name"], c["value"]) for c in cookies)
