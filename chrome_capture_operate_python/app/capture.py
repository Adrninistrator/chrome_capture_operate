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
import re
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlsplit

import websockets

from . import chrome_proc
from . import cookie_cdp
from .config import CAPTURE_DIR, DEFAULT_OP_EVENT_TYPES, OP_EVENT_TYPES

log = logging.getLogger("app.capture")

STATE_IDLE = "idle"
STATE_CAPTURING = "capturing"
STATE_PAUSED = "paused"

MARK_SUFFIX = "_人工标记"

# DOM 操作监听注入脚本模板（优化建议 2026-09-27 建议 1 补充 / DOM事件监听注入）。
# build_op_inject_js() 将 __OP_TYPES__ 占位符替换为启用的事件类型清单后
# 注入；未启用的类型**不注册监听**——无页面事件开销、无 CDP 回传流量
# （参数配置 conf.json 的 op_event_types，见 app/config.py 默认清单）。
# 经 Page.addScriptToEvaluateOnNewDocument 注入到每个 attach 的页面会话，
# 新文档（导航/刷新/新标签页）自动执行。事件经 console.log("[OP] ...")
# 回传（Runtime.consoleAPICalled），服务端解析后写入 note 时间线。
# 带版本号与可清理 handler 引用（实测发现：匿名监听器无法移除、already
# 守卫会挡住升级版注入）。版本号内嵌事件类型签名且精确相等比较——
# 配置变更后重注入时先清理旧监听再按新清单安装（不会残留旧类型监听）。
# 人工确认的决策（2026-09-27，均"同意建议"）：
# - 只记 change 不记 input（避免框架初始化噪音；值仍完整）
# - 记录合成事件（isTrusted=false）并标注"合成"
# - 密码框掩码为 ***
# - 导航信息全加（启动记录 + pagehide + SPA 路由）
# - 回车键补记焦点输入框的值：回车直接提交的登录/搜索场景输入框
#   未失焦、change 不触发（实测火山引擎登录页），补一条值快照，
#   密码仍掩码，isTrusted 跟随回车键事件
OP_INJECT_JS = r"""(function(){
var T=[__OP_TYPES__];var V='1.3|'+T.join(',');
if(window.__opV===V)return'old';
if(window.__opCleanup)window.__opCleanup();
window.__opV=V;
window.__opLog=[];
var H={};
function on(t){return T.indexOf(t)>=0;}
function now(){var d=new Date();var ds=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');return ds+' '+d.toTimeString().slice(0,8)+'.'+String(d.getMilliseconds()).padStart(3,'0');}
function desc(el){
if(!el||!el.tagName)return'(unknown)';
var t=el.tagName.toLowerCase(),id=el.id?'#'+el.id:'',cls='';
if(typeof el.className==='string'&&el.className.trim())cls='.'+el.className.trim().split(/\s+/).slice(0,2).join('.');
var lb='';
try{lb=(el.getAttribute('aria-label')||el.placeholder||(el.textContent||'').trim()).slice(0,40);}catch(e){}
var row=el.closest&&el.closest('[class*=el-col],[class*=form-item],label,tr');
var ctx=row?(row.textContent||'').trim().slice(0,30):'';
return t+id+cls+(lb?' "'+lb+'"':'')+(ctx?' @'+ctx:'');}
function emit(e){try{window.__opLog.push(e);if(window.__opLog.length>500)window.__opLog.shift();console.log('[OP] '+JSON.stringify(e));}catch(ex){}}
function chg(el,tr){
var e={time:now(),type:'change',isTrusted:tr,target:desc(el)};
if(el&&el.value!==undefined)e.value=el.type==='password'?'***':String(el.value).slice(0,80);
if(el&&(el.type==='checkbox'||el.type==='radio'))e.checked=el.checked;
emit(e);}
function log(type,ev){
var el=(ev.composedPath&&ev.composedPath()[0])||ev.target;
if(type==='change'){chg(el,ev.isTrusted);return;}
var e={time:now(),type:type,isTrusted:ev.isTrusted,target:desc(el)};
if(type==='keydown')e.key=ev.key;
if(type==='copy'||type==='paste')e.selection=String(document.getSelection()||'').slice(0,40);
if(type==='submit'&&el&&el.tagName==='FORM')e.form=(el.getAttribute('action')||'(当前页)')+' method='+(el.method||'get');
emit(e);}
['click','pointerdown','change','submit','contextmenu','copy','paste','focusin'].forEach(function(t){
if(!on(t))return;H[t]=function(e){log(t,e);};document.addEventListener(t,H[t],true);});
if(on('keydown')){H.keydown=function(e){
var k=e.key;
if(k==='Enter'&&on('change')){var ae=document.activeElement;
if(ae&&ae.tagName==='INPUT'&&ae.type!=='checkbox'&&ae.type!=='radio'&&ae.value!==undefined)chg(ae,e.isTrusted);}
if(k==='F5'||(e.ctrlKey&&(k==='r'||k==='R'))||k==='Enter'||k==='Escape')log('keydown',e);};
document.addEventListener('keydown',H.keydown,true);}
if(on('scroll')){H.scroll=function(){emit({time:now(),type:'scroll',isTrusted:true,target:location.href.slice(0,60)});};
window.addEventListener('scroll',H.scroll,true);}
if(on('pagehide')){H.pagehide=function(){emit({time:now(),type:'pagehide',isTrusted:true,target:location.href.slice(0,80)});};
window.addEventListener('pagehide',H.pagehide,true);}
var op,or;
if(on('route')){H.popstate=function(){emit({time:now(),type:'route',isTrusted:true,target:'popstate '+location.href.slice(0,80)});};
window.addEventListener('popstate',H.popstate,true);
H.hashchange=function(e){emit({time:now(),type:'route',isTrusted:true,target:'hash '+(e.newURL||'').slice(0,80)});};
window.addEventListener('hashchange',H.hashchange,true);
op=history.pushState;or=history.replaceState;
history.pushState=function(){emit({time:now(),type:'route',isTrusted:true,target:'pushState '+String(arguments[2]||'').slice(0,80)});return op.apply(history,arguments);};
history.replaceState=function(){emit({time:now(),type:'route',isTrusted:true,target:'replaceState '+String(arguments[2]||'').slice(0,80)});return or.apply(history,arguments);};}
if(on('page_load')){var nav=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];
emit({time:now(),type:'page_load',isTrusted:true,target:(nav&&nav.type||'unknown')+' '+location.href.slice(0,80)+(document.referrer?' referrer='+document.referrer.slice(0,60):'')});}
window.__opCleanup=function(){
['click','pointerdown','change','submit','contextmenu','copy','paste','focusin','keydown'].forEach(function(t){if(H[t])document.removeEventListener(t,H[t],true);});
if(H.scroll)window.removeEventListener('scroll',H.scroll,true);
if(H.pagehide)window.removeEventListener('pagehide',H.pagehide,true);
if(H.popstate)window.removeEventListener('popstate',H.popstate,true);
if(H.hashchange)window.removeEventListener('hashchange',H.hashchange,true);
if(op)history.pushState=op;if(or)history.replaceState=or;
delete window.__opV;delete window.__opCleanup;};
return'installed '+V;})()"""


def build_op_inject_js(types):
    """按启用的事件类型清单生成注入脚本（未知类型忽略，按
    OP_EVENT_TYPES 规范序去重）。

    清单为空（或全部未知）返回 None——等价于该页面不记录操作。
    类型清单嵌进脚本版本号：配置变更后重注入先清理旧监听再安装。"""
    wanted = set(t for t in (types or []) if t in OP_EVENT_TYPES)
    if not wanted:
        return None
    return OP_INJECT_JS.replace(
        "__OP_TYPES__", ",".join('"%s"' % t
                                 for t in OP_EVENT_TYPES if t in wanted))

# 延迟注入参数：attach 由 targetInfoChanged 在导航开始时触发（URL 已变、
# 文档未提交，当前文档仍是 about:blank/旧文档）——注入延迟到文档提交后
# 由后台任务执行（探测间隔/等待上限见 verify_attach_timing.py 实测依据）
_INJECT_PROBE_INTERVAL = 0.3
_INJECT_COMMIT_WAIT_SEC = 30.0
# 历史操作事件过滤容差（秒）：事件时间早于会话开始减本值视为控制台
# 缓冲重放的历史事件，丢弃（问题记录 2026-09-28 问题 4）
_STALE_OP_TOLERANCE = 2.0

TRIGGER_LABELS = {"web": "人工（网页）", "mcp": "AI Agent（MCP工具）"}


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


def session_path(name):
    """抓包记录子目录名合法性校验：不含路径分隔符、非 . / ..，
    解析后位于抓包根目录内且存在。返回绝对路径，非法/不存在返回 None。
    （Web 抓包记录页与 MCP 查询/删除被过滤记录工具共用）"""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    root = os.path.abspath(CAPTURE_DIR)
    p = os.path.abspath(os.path.join(root, name))
    if not p.startswith(root + os.sep):
        return None
    return p if os.path.isdir(p) else None


def _url_key(row):
    """接口标识（判"同一接口"而非"同一请求"，优化建议 2026-09-27 建议 3）：
    method + host + path，去掉 query 参数。"""
    try:
        parts = urlsplit(row.get("url", "") or "")
    except ValueError:
        return (row.get("method", ""), row.get("url", "") or "")
    return (row.get("method", ""), (parts.netloc or "") + (parts.path or ""))


def _norm_header_value(v):
    """头值落盘规范化（优化建议 2026-09-27 建议 6.4）：值中的换行替换为
    ", "——真实案例 Kss-Upstream 的值含两个 IP 分两行，按行解析的消费者
    会误判头结构。数组形态的值（ExtraInfo 偶发）拼接为字符串。"""
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    elif not isinstance(v, str):
        v = "" if v is None else str(v)
    return v.replace("\r\n", ", ").replace("\n", ", ").replace("\r", ", ")


def _sanitize_op_text(text):
    """操作文本落盘清洗：\\r\\n\\t 替换为空格、| 替换为全角｜——前端操作
    的目标描述/输入值可能含这些字符，会破坏 index.md 的表格行结构与
    parse_index_records 的按行/按列解析。"""
    return re.sub(r"[\r\n\t]+", " ", str(text)).replace("|", "｜")


def _parse_op_time(entry):
    """解析注入脚本回传的事件时间（页面时钟，与服务器同机同系统时钟）
    为 epoch 秒——事件行时间线取真实事件时刻而非回传到达时刻，
    消除 WebSocket 交付延迟（毫秒~数十毫秒）对排序的影响。

    旧版脚本/字段缺失/解析失败返回 None，调用方回落到达时刻。"""
    try:
        dt = datetime.strptime(str(entry.get("time", "")),
                               "%Y-%m-%d %H:%M:%S.%f")
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def parse_index_records(index_path):
    """解析 index.md 汇总表行为结构化记录。

    跳过表头/分隔行与 > 注释行（旧格式残留）。行分为两类：
    - 请求行：资源类型为 XHR/Document 等正常值，seq 匹配 {seq}.md
    - 操作行：资源类型为"操作"（DOM 事件/AI note/页面异常），
      seq/method/首次/content-type/body字节数/耗时 为占位值

    兼容 9 列（旧格式，无"首次"列）与 10 列（含"首次"列，建议 3）。
    （Web 抓包记录页与 MCP 删除被过滤记录工具共用）"""
    records = []
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if (not line.startswith("|")
                        or line.startswith("| 序号")
                        or line.startswith("|---")):
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) not in (9, 10):
                    continue
                wide = len(cells) == 10
                is_op = cells[3] == "操作"
                try:
                    records.append({
                        "seq": cells[0],
                        "time": "" if cells[1] == "-" else cells[1],
                        "method": cells[2],
                        "type": "" if cells[3] == "-" else cells[3],
                        "is_op": is_op,
                        "first_seen": (wide and cells[4] == "★"
                                       and not is_op),
                        "url": cells[5] if wide else cells[4],
                        "content_type": (cells[6] if wide else cells[5]),
                        "req_size": int(cells[7] if wide else cells[6]),
                        "resp_size": int(cells[8] if wide else cells[7]),
                        "duration_ms": int(cells[9] if wide else cells[8]),
                    })
                except (ValueError, IndexError):
                    continue
    except OSError:
        pass
    return records


def list_history_sessions():
    """列出抓包记录目录下的全部子目录（新在前，按目录名倒序）。
    每项含名称、记录数、完整路径、是否人工标记。
    （Web 抓包记录页与 MCP 查询抓包目录下的子目录工具共用）"""
    items = []
    for d in sorted((d for d in os.listdir(CAPTURE_DIR)
                     if os.path.isdir(os.path.join(CAPTURE_DIR, d))),
                    reverse=True):
        count = 0
        try:
            for f in os.listdir(os.path.join(CAPTURE_DIR, d)):
                # 记录文件为 %010d.md（index.md 不计入）
                if re.fullmatch(r"[0-9]{10}\.md", f):
                    count += 1
        except OSError:
            pass
        items.append({"name": d, "record_count": count,
                      "path": os.path.join(CAPTURE_DIR, d),
                      "marked": d.endswith(MARK_SUFFIX)})
    return items


def purge_history_session(session_dir, session_name, capture_conf, config):
    """按过滤条件删除已结束会话目录中被命中的记录（同步，建议放后台线程）。

    删记录文件（.md 及同名二进制文件）、按保留行重写 index.md（序号
    不变，"首次"列按保留行重新计算——首现行被删时★顺延到下一行；
    操作行在表格内且不参与过滤始终保留；旧格式会话的"## 操作注释"
    小节原样保留）。与抓包中 purge 的评估口径一致
    （evaluate_violation 共用）。返回 (deleted, kept)；目录无 index.md
    时返回 (0, 0)。（Web 抓包记录页与 MCP 删除被过滤的记录工具共用）
    """
    index_path = os.path.join(session_dir, "index.md")
    records = parse_index_records(index_path)
    # 操作行（type="操作"）不参与过滤，始终保留
    victims = [r for r in records
               if not r.get("is_op")
               and evaluate_violation(r, capture_conf, config)]
    keep = [r for r in records
            if r.get("is_op")
            or not evaluate_violation(r, capture_conf, config)]
    for r in victims:
        seq = r["seq"]
        try:
            for f in os.listdir(session_dir):
                if f.startswith(seq + "."):
                    try:
                        os.remove(os.path.join(session_dir, f))
                    except OSError as e:
                        log.warning("删除文件失败 %s: %s", f, e)
        except OSError:
            pass
    # 旧格式会话（统一时间线改造前）的"## 操作注释"小节原样保留
    # （从标记起到文件尾）；新格式操作行在表格内，经 is_op 已全部保留
    tail = ""
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            raw = f.read()
        i = raw.find("## 操作注释")
        if i >= 0:
            tail = raw[i:]
    except OSError:
        pass
    lines = ["# 抓包汇总 %s\n\n" % session_name
             + "| 序号 | 请求时间 | 方法 | 资源类型 | 首次 | URL | 返回content-type | "
               "请求body字节数 | 返回body字节数 | 耗时(ms) |\n"
             + "|---|---|---|---|---|---|---|---|---|---|\n"]
    seen = set()
    for row in keep:
        if row.get("is_op"):
            row["first_seen"] = False   # 操作行不参与"首次"标记
        else:
            key = _url_key(row)
            row["first_seen"] = key not in seen
            seen.add(key)
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n" % (
            row["seq"], row.get("time") or "-",
            row.get("method", ""), row.get("type") or "-",
            "★" if row["first_seen"] else "",
            row.get("url", ""), row.get("content_type", ""),
            row.get("req_size", 0), row.get("resp_size", 0),
            row.get("duration_ms", 0)))
    if tail:
        lines.append("\n" + tail)
    with open(index_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    log.info("删除已结束会话被过滤的记录(%s): 删 %d 留 %d",
             session_name, len(victims), len(keep))
    return len(victims), len(keep)


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


# 会话目录名的时间戳前缀（yyyy_MM_dd_HH_mm_ss，19 字符）
_TS_PREFIX_RE = re.compile(r"^\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}")
# 会话目录描述后缀的 Windows 非法字符与控制字符（重命名时过滤）
_DESC_INVALID_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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
            # 常见为 Chrome 进程退出：对端直接消失、未走 WebSocket 关闭
            # 握手（websockets 报 "no close frame received or sent"）
            log.info("CDP 连接断开（通常为 Chrome 进程退出）: %s", e)
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
        self.trigger = None    # 当前会话触发方式：web=人工（网页）/mcp=AI Agent（MCP工具）
        self.session_dir = None
        self.counter = 0
        self.pending = {}        # (session_id, request_id) -> dict
        self._extra_req = {}     # key -> 请求原始头（先于主事件到达的暂存）
        self._extra_resp = {}    # key -> 响应原始头
        self._attached = set()   # 已手动 attach 的 targetId
        self.seen_hosts = {}     # host -> first_seen_ts（CDP 连接后持续收集）
        self.records = []        # 当前会话已落盘的记录行（row dict），序号不变
        self.marked = False      # 当前会话是否被人工标记（结束后目录加后缀）
        self.op_counter = 0      # 操作行序号计数（fr%08d，独立于请求序号）
        self.session_start_wall = 0   # 会话开始时刻（历史操作事件过滤基准）
        self._stale_ops_dropped = 0   # 丢弃的缓冲重放历史操作事件数
        self.notes = []          # 当前会话操作行 [(seq, wall_time, text)]
        self.on_event = None     # async broadcast(dict)，由 webapp 注入
        self.on_tray_notify = None  # 托盘气泡通知回调(message)，由 webapp 注入
        self.ops_inject = False  # 会话级 DOM 操作监听注入开关（start 时经三入口设置）
        self._injected = {}      # session_id -> 注入脚本 identifier（stop 时拆除）
        self._tid_sid = {}       # targetId -> sessionId（重注入按触发 target 定向）
        self._inject_tasks = {}  # session_id -> 延迟注入任务（stop 时取消）
        # 会话级抓包配置（不写 conf.json）
        self.capture_conf = {
            "suffix_enabled": True,
            "content_type_enabled": True,
            "type_enabled": True,  # 根据 Chrome 资源类型过滤（清单在 conf.json）
            "domains": [],        # 空 = 全部
            "uri_rules": [],      # [{type: prefix|equals|contains|suffix, value}]
            "ws_capture": False,  # 暂不实现
            "ops_inject_enabled": True,  # DOM 操作监听注入（抓包配置卡勾选框）
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
            "trigger": self.trigger,
        }

    def clear_hosts(self):
        """清空累计访问过的网址清单（seen_hosts）。

        Web 抓包配置页"清除记录"按钮与 MCP clear_hosts 工具共用；
        清空后"需要抓包的网址"回到全选（不过滤=全部抓包）。新访问的
        网址会继续累积收集。
        """
        n = len(self.seen_hosts)
        self.seen_hosts.clear()
        log.info("已清除累计访问过的网址清单（%d 个）", n)
        return n

    async def _broadcast(self, payload):
        if self.on_event:
            try:
                await self.on_event(payload)
            except Exception:
                log.exception("广播失败")

    def _tray_notify(self, message):
        """托盘气泡通知（prompt 需求：开始/结束抓包后在系统托盘图标
        气泡显示，Web 与 MCP 触发均经此路径）。回调由 webapp 注入
        （main.py 经托盘 icon.notify 实现 Shell_NotifyIcon 气泡），
        未注入（无托盘/测试）时静默跳过，通知失败不影响抓包流程。"""
        cb = self.on_tray_notify
        if cb is None:
            return
        try:
            cb(message)
        except Exception as e:
            log.warning("托盘气泡通知回调失败: %s", e)

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
        # Authorization 观察器（旁路 handler，与上面 capture 的同名 handler
        # 并行、互不影响；注册在 ensure_cdp 内保证 CDPClient 重建后仍存活）：
        # 抓包期间观察请求头中的 Authorization 按主机缓存于内存，供
        # /api/cookies/cdp 查询（不落盘不落日志，见 app/cookie_cdp.py）
        self.cdp.on("Network.requestWillBeSent",
                    cookie_cdp.auth_cache.on_request)
        self.cdp.on("Network.requestWillBeSentExtraInfo",
                    cookie_cdp.auth_cache.on_request_extra)
        # DOM 操作监听事件回传（旁路 handler，同 Authorization 观察器
        # 模式）：[OP] 前缀的 console.log → note 时间线；页面 JS 异常
        # → note 时间线（优化建议 2026-09-27 建议 1 补充 + 决策 8）
        self.cdp.on("Runtime.consoleAPICalled", self._on_console_op)
        self.cdp.on("Runtime.exceptionThrown", self._on_page_error)
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
        self.trigger = None
        self._attached.clear()
        self._tid_sid.clear()
        self._injected.clear()
        # 取消等待文档提交的延迟注入任务（连接已断，注入无意义）
        for task in self._inject_tasks.values():
            task.cancel()
        self._inject_tasks.clear()
        # 连接断开时在途请求同样落盘，避免记录丢失
        pending = list(self.pending.values())
        self.pending.clear()
        self._extra_req.clear()
        self._extra_resp.clear()
        mark_err = None   # 仅抓包中断开时才会有标记结果
        if was in (STATE_CAPTURING, STATE_PAUSED):
            for rec in pending:
                if not rec.get("response"):
                    rec["error"] = "Chrome 连接断开时响应未完成"
                await self._write_record(rec, None, None)
            # 断开即会话结束：与 stop() 同口径全量按时间排序重写 index.md
            await asyncio.to_thread(self._rewrite_index)
            mark_err = self._apply_mark_suffix()
            log.info("Chrome 调试端口断开，抓包已结束: %s", self.session_name)
            self._tray_notify("抓包已停止: %s（Chrome 进程已结束）"
                              % self.session_name)
        else:
            log.info("Chrome 调试端口断开（当前未在抓包，状态保持空闲）")
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status),
                               "message": ("Chrome 进程已结束，抓包已停止"
                                           if was in (STATE_CAPTURING, STATE_PAUSED)
                                           else "Chrome 进程已结束")})
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
        if not tid:
            return
        if tid in self._attached:
            # 已 attach 页面的两个注入场景（只处理触发 target 的 session，
            # 不对全部已注入页面广播）：
            # ① 会话中刷新/导航（targetInfoChanged 触发）：本会话已注入
            #    （sid in _injected）→ 重新 evaluate 注入——刷新后 JS 上下
            #    文件销毁重建，旧注入失效（addScriptToEvaluateOnNewDocument
            #    对同 target 的刷新与注册时已在途的导航均不生效，由此兜底）
            # ② 新会话沿用上一会话的 attach（stop 拆除了注入、_injected
            #    已清空，但 _tid_sid 保留了映射——问题记录 2026-09-28
            #    问题 1：此前此处拿不到 sid 直接跳过，二次抓包注入静默
            #    失效）→ 走延迟注入任务补注入（含 Runtime.enable，文档
            #    提交后执行）
            sid = self._tid_sid.get(tid)
            if self.ops_inject and sid and self.cdp and self.cdp.ws:
                if sid in self._injected:
                    js = self._op_inject_js()
                    if js:
                        try:
                            r_eval = await self.cdp.cmd(
                                "Runtime.evaluate",
                                {"expression": js,
                                 "returnByValue": True}, session_id=sid)
                            result = str(r_eval.get("result", {}).get("value"))
                            if "installed" in result:
                                log.info("页面刷新后重新注入: %s", tid)
                        except Exception:
                            pass  # 会话可能已失效，忽略
                elif sid not in self._inject_tasks:
                    self._inject_tasks[sid] = asyncio.create_task(
                        self._inject_ops_when_committed(sid, tid))
            return
        self._attached.add(tid)
        try:
            r = await self.cdp.cmd("Target.attachToTarget",
                                   {"targetId": tid, "flatten": True})
            sid = r.get("sessionId")
            self._tid_sid[tid] = sid
            await self.cdp.cmd("Network.enable", session_id=sid)
            # DOM 操作监听注入（会话级开关，优化建议 2026-09-27 建议 1 补充）。
            # attach 由 targetInfoChanged 在导航开始时触发——URL 已变 http
            # 但文档未提交（当前文档仍是 about:blank/旧文档），立即注入会
            # 落在即将销毁的上下文（tests/verify_attach_timing.py 实测：
            # 窗口期命中时注入 100% 落在 about:blank，真实文档的覆盖全靠
            # 后续 targetInfoChanged 重注入旁路，依赖标题变化事件）。故
            # 注入整体延迟到文档提交后由后台任务执行，不阻塞 CDP 事件
            # 串行分发（Network.enable 保持立即，导航期间请求不丢）。
            if self.ops_inject:
                self._inject_tasks[sid] = asyncio.create_task(
                    self._inject_ops_when_committed(sid, tid))
            log.info("已 attach: %s %s", tid, tinfo.get("url", "")[:60])
        except Exception as e:
            self._attached.discard(tid)
            self._tid_sid.pop(tid, None)
            log.warning("attach 失败 %s: %s", tid, e)

    def _op_inject_js(self):
        """按参数配置的 op_event_types 生成注入脚本（清单为空返回 None）。

        读取当前配置值（非会话快照）：参数配置保存后，新注入/重注入的
        页面即时生效；已注入页面到下一次重注入（刷新/导航）时生效。"""
        types = self.config.get("op_event_types")
        if types is None:
            types = DEFAULT_OP_EVENT_TYPES
        return build_op_inject_js(types)

    async def _probe_doc_committed(self, sid):
        """探测会话当前文档是否已提交（导航窗口期 location 仍是
        about:blank/旧文档）。

        Runtime.evaluate 无需 Runtime.enable 即可执行；探测失败按已
        提交处理（兜底：不因探测异常反而丢掉注入机会）。"""
        try:
            r = await self.cdp.cmd(
                "Runtime.evaluate",
                {"expression": "String(location.href)",
                 "returnByValue": True}, session_id=sid)
            href = str(r.get("result", {}).get("value") or "")
        except Exception:
            return True
        return href.startswith(("http://", "https://"))

    async def _inject_ops_when_committed(self, sid, tid):
        """后台任务：等文档提交后注入 DOM 操作监听（attach 时创建）。

        stop/_on_cdp_close/_on_target_destroyed 经 _inject_tasks 取消。
        超时（文档迟迟未提交，如出错的挂起导航）放弃当前文档注入，仅
        注册通道②供后续文档使用并告警。"""
        try:
            deadline = time.monotonic() + _INJECT_COMMIT_WAIT_SEC
            while time.monotonic() < deadline:
                if self.state not in (STATE_CAPTURING, STATE_PAUSED):
                    return          # 会话已结束（stop 已拆注入）
                if await self._probe_doc_committed(sid):
                    await self._inject_ops(sid, tid)
                    return
                await asyncio.sleep(_INJECT_PROBE_INTERVAL)
            log.warning("文档 %s 在 %.0fs 内未提交，跳过当前页面注入"
                        "（后续文档经自动注入覆盖）",
                        tid, _INJECT_COMMIT_WAIT_SEC)
            js = self._op_inject_js()
            if js is None:
                return
            try:
                r2 = await self.cdp.cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": js}, session_id=sid)
                self._injected[sid] = r2.get("identifier")
            except Exception as e:
                log.warning("操作监听注入失败 %s: %s", tid, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("操作监听注入失败 %s: %s", tid, e)
        finally:
            self._inject_tasks.pop(sid, None)

    async def _inject_ops(self, sid, tid):
        """注入 DOM 操作监听（文档已提交后调用）：

        通道① Runtime.enable（接收 consoleAPICalled/exceptionThrown
        事件）+ Runtime.evaluate 向当前文档立即注入；通道②
        Page.addScriptToEvaluateOnNewDocument 注册后续新文档。注入
        脚本按参数配置的事件类型清单生成、带版本守卫（重复注入/
        配置变更后重注入均安全：'old' 跳过或先清理再安装）。"""
        js = self._op_inject_js()
        if js is None:
            log.info("事件类型清单为空，跳过操作监听注入 %s", tid)
            return
        # 先清空页面控制台缓冲再 enable：Runtime.enable 会重放 V8 控制台
        # 存储的历史消息（Chrome 为 DevTools 晚接入保留的缓冲）——
        # 历史会话/无会话期间遗留监听器产生的 [OP] 日志会被整段重放并
        # 写入新会话（问题记录 2026-09-28 问题 4：新会话 index.md 被前
        # 一天的历史操作淹没）。命令失败时由事件时间兜底过滤。
        try:
            await self.cdp.cmd("Runtime.discardConsoleEntries",
                               session_id=sid)
        except Exception:
            pass
        await self.cdp.cmd("Runtime.enable", session_id=sid)
        try:
            r_eval = await self.cdp.cmd(
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
                session_id=sid)
            log.info("当前页面注入: %s",
                     r_eval.get("result", {}).get("value"))
        except Exception as e:
            log.warning("当前页面注入失败 %s: %s", tid, e)
        # 后续新文档自动注入。实测注意：对注册时已在途的导航与同
        # target 的刷新不生效，由 targetInfoChanged 重注入兜底。
        r2 = await self.cdp.cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": js}, session_id=sid)
        self._injected[sid] = r2.get("identifier")

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
        tid = params.get("targetId")
        self._attached.discard(tid)
        sid = self._tid_sid.pop(tid, None)
        if sid:
            task = self._inject_tasks.pop(sid, None)
            if task:
                task.cancel()      # target 已销毁，等待注入无意义
            # 会话已随 target 失效，stop 时无需再拆除
            self._injected.pop(sid, None)

    def check_source(self, source):
        """检查操作来源与当前抓包会话的触发方式是否一致。

        prompt 需求「避免人工与AI同时操作的处理」：开始抓包时记录触发
        方式（web=人工通过网页HTTP接口 / mcp=AI Agent通过MCP工具），
        之后暂停/继续/结束抓包、修改抓包配置、人工标记等操作需与触发
        方式一致，避免相互影响。source 为 None 表示内部调用（如 CDP
        断开自动结束），不受限；会话未开始时不限制。
        """
        if source is None or self.state == STATE_IDLE or not self.trigger:
            return True, None
        if self.trigger != source:
            who = TRIGGER_LABELS.get(self.trigger, self.trigger)
            actor = TRIGGER_LABELS.get(source, source)
            return False, ("当前抓包由%s触发，%s不能执行当前操作，"
                           "避免人工与AI同时操作" % (who, actor))
        return True, None

    # ---------- 抓包控制 ----------
    async def start(self, source="web", inject_ops=None):
        if self.state == STATE_CAPTURING:
            return False, "已在抓包中"
        if self.state == STATE_PAUSED:
            return False, "抓包处于暂停中，请继续抓包或结束抓包"
        cdp_port = self.config.get("cdp_port")
        if not chrome_proc.is_cdp_alive(cdp_port):
            return False, "监听调试端口的 Chrome 未启动，请先启动用于抓包的 Chrome 进程"
        if not await self.ensure_cdp():
            return False, "CDP 连接失败，请确认 Chrome 已以调试端口启动"
        # 注入开关（三入口一标志）：显式传参 > capture_conf 的会话级键
        #（默认 True，前端勾选框控制）> True
        if inject_ops is None:
            inject_ops = self.capture_conf.get("ops_inject_enabled", True)
        self.ops_inject = inject_ops
        self._injected.clear()
        # _tid_sid 与 _attached 不清空：CDP 连接与 target 会话跨抓包会话
        # 复用（问题记录 2026-09-28 问题 1：清空后二次抓包对已 attach
        # 页面拿不到 session，inject_ops 静默失效）；_attach_existing 会
        # 对已 attach 页面补注入，连接断开时 _on_cdp_close 统一清理
        for task in self._inject_tasks.values():
            task.cancel()
        self._inject_tasks.clear()
        self.session_name = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        self.session_dir = os.path.join(CAPTURE_DIR, self.session_name)
        os.makedirs(self.session_dir, exist_ok=True)
        self.counter = 0
        self.op_counter = 0
        self.session_start_wall = time.time()
        self._stale_ops_dropped = 0
        self.pending.clear()
        self._extra_req.clear()
        self._extra_resp.clear()
        self.records.clear()
        self.marked = False       # 新会话重置标记
        self.notes.clear()        # 新会话重置操作注释
        self.trigger = source
        self.state = STATE_CAPTURING

        # attach 所有已存在且有真实 URL 的页面（抓包期间新开的页面由
        # targetInfoChanged 触发 _maybe_attach）
        await self._attach_existing()
        with open(os.path.join(self.session_dir, "index.md"), "w",
                  encoding="utf-8") as f:
            f.write(self._index_header())
        log.info("开始抓包: %s", self.session_name)
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        self._tray_notify("抓包已开始: %s" % self.session_name)
        return True, "抓包已开始: %s" % self.session_name

    async def _attach_existing(self):
        try:
            r = await self.cdp.cmd("Target.getTargets")
            for t in r.get("targetInfos", []):
                await self._maybe_attach(t)
        except Exception as e:
            log.warning("枚举 target 失败: %s", e)

    async def set_ops_inject(self, enabled, source=None):
        """抓包中途开关 DOM 操作监听注入（会话级，即时生效）。

        开启：对已 attach 但未注入的页面（开启前 attach 的）立即补注入
        （延迟注入任务，文档提交后执行）；后续新开/导航页面照常注入。
        关闭：移除自动注入脚本，并调用注入脚本自带的 __opCleanup 拆除
        页面监听（已写入时间线的操作保留）；后续页面不再注入。
        未在抓包时仅更新 capture_conf 默认键（下次开始抓包生效）。
        Web 勾选框（PUT /api/capture/config）与 MCP set_capture_filters
        共用本入口；capture_conf 的 ops_inject_enabled 同步更新。
        """
        if self.state in (STATE_CAPTURING, STATE_PAUSED):
            ok, err = self.check_source(source)
            if not ok:
                return False, err
        enabled = bool(enabled)
        self.capture_conf["ops_inject_enabled"] = enabled
        if self.state not in (STATE_CAPTURING, STATE_PAUSED):
            return True, "未在抓包，已更新配置（下次开始抓包生效）"
        if enabled == self.ops_inject:
            return True, "当前已是该状态"
        if enabled:
            self.ops_inject = True
            # 已 attach 但未注入的页面（开启注入前 attach 的）补注入
            for tid, sid in list(self._tid_sid.items()):
                if (sid not in self._injected
                        and sid not in self._inject_tasks):
                    self._inject_tasks[sid] = asyncio.create_task(
                        self._inject_ops_when_committed(sid, tid))
            return True, "已开启前端页面操作记录，已打开页面即时注入"
        # 关闭：拆已注入页面的监听与自动注入脚本
        self.ops_inject = False
        for task in self._inject_tasks.values():
            task.cancel()
        self._inject_tasks.clear()
        for sid, identifier in list(self._injected.items()):
            if self.cdp and self.cdp.ws:
                for method, params in (
                        ("Page.removeScriptToEvaluateOnNewDocument",
                         {"identifier": identifier}),
                        ("Runtime.evaluate",
                         {"expression":
                          "window.__opCleanup&&window.__opCleanup()",
                          "returnByValue": True})):
                    try:
                        await self.cdp.cmd(method, params, session_id=sid)
                    except Exception:
                        pass  # 会话可能已失效，忽略
        self._injected.clear()
        return True, "已关闭前端页面操作记录（已记录的操作保留）"

    async def pause(self, source=None):
        """暂停抓包：不清空当前显示的记录，之后可以继续。"""
        if self.state != STATE_CAPTURING:
            return False, "当前未在抓包，无法暂停"
        ok, err = self.check_source(source)
        if not ok:
            return False, err
        self.state = STATE_PAUSED
        log.info("暂停抓包: %s", self.session_name)
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        return True, "抓包已暂停（记录保留，可继续）"

    async def resume(self, source=None):
        """继续抓包：从暂停恢复，沿用当前会话目录与序号。"""
        if self.state != STATE_PAUSED:
            return False, "当前未处于暂停状态"
        ok, err = self.check_source(source)
        if not ok:
            return False, err
        self.state = STATE_CAPTURING
        log.info("继续抓包: %s", self.session_name)
        # 暂停期间新打开的页面可能未 attach，恢复时补挂
        await self._attach_existing()
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        return True, "抓包已继续: %s" % self.session_name

    async def mark(self, source=None):
        """标记当前抓包会话：本次结束后目录名增加 _人工标记 后缀。"""
        if self.state not in (STATE_CAPTURING, STATE_PAUSED):
            return False, "当前未在抓包，无法标记"
        if self.marked:
            return False, "当前会话已标记"
        ok, err = self.check_source(source)
        if not ok:
            return False, err
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

    def rename_session_suffix(self, session=None, name=None, mark=True):
        """会话目录增加/修改描述后缀（MCP rename_capture_session 的核心，
        Web 端标记/完整重命名各有独立入口）。

        目录名结构 {时间戳}[_{描述后缀}][_人工标记]：时间戳前缀始终保留
        不修改；描述后缀无则新增、有则替换（不叠加）；mark 控制人工标记
        后缀（true=确保存在，false=移除）。session 为空时取最近结束的
        会话（session_name 跟踪重命名，见 stop 后 _apply_mark_suffix 同
        口径）。返回 (ok, {name, path, changed} 或 error str)。
        """
        session = (session or "").strip() or self.session_name
        if not session:
            return False, "未指定会话目录，且无最近结束的会话可重命名"
        if os.path.basename(session) != session or session in (".", ".."):
            return False, "会话目录名非法"
        p = os.path.join(CAPTURE_DIR, session)
        if not os.path.isdir(p):
            return False, "会话目录不存在: %s" % session
        if (self.state != STATE_IDLE and session == self.session_name):
            return False, "正在抓包中的会话目录不允许重命名，请先结束抓包"
        m = _TS_PREFIX_RE.match(session)
        if not m:
            return False, ("目录名无时间戳前缀，无法按后缀模式重命名"
                           "（可用 Web 页面的完整重命名）")
        ts = m.group(0)
        base = session[len(ts):]
        has_mark = base.endswith(MARK_SUFFIX)
        if has_mark:
            base = base[:-len(MARK_SUFFIX)]
        desc = base[1:] if base.startswith("_") else base
        if name is not None:
            # 清洗非法字符/控制字符与首尾空格，上限 40 字符；
            # 清洗后为空视为未提供（保持现有描述后缀）
            clean = _DESC_INVALID_RE.sub("", str(name)).strip()[:40]
            if clean:
                desc = clean
        new = ts
        if desc:
            new += "_" + desc
        if mark:
            new += MARK_SUFFIX
        if new == session:
            return True, {"name": new, "path": p, "changed": False}
        new_path = os.path.join(CAPTURE_DIR, new)
        if os.path.exists(new_path):
            return False, "已存在同名目录: %s" % new
        ok, err = rename_with_retry(p, new_path)
        if not ok:
            return False, "重命名失败: %s" % err
        if session == self.session_name:
            # 跟踪最近会话（空 session 的后续调用与状态展示保持一致）
            self.session_name = new
            self.session_dir = new_path
        log.info("会话目录已重命名: %s -> %s", session, new)
        return True, {"name": new, "path": new_path, "changed": True}

    async def note(self, text, source=None):
        """抓包期间记录一条操作注释（时间点留痕，写入 index.md 统一时间线）。

        优化建议 2026-09-27 建议 1：AI/人工在前端页面执行操作前后留痕，
        解决"前端操作 ↔ 请求"只能靠时间戳+回忆对齐的问题。与 mark（会话
        目录改名加后缀，标记重要会话）语义不同。抓包期间 index.md 以表格行
        追加（与请求行统一格式，由 _index_lock 保护）；结束时 _rewrite_index
        按时间排序重写。注释经 WS 广播（type=note），页面实时可见。
        """
        if self.state not in (STATE_CAPTURING, STATE_PAUSED):
            return False, "当前未在抓包，无法记录注释"
        ok, err = self.check_source(source)
        if not ok:
            return False, err
        text = (text or "").strip()[:200]      # 截断防滥用
        if not text:
            return False, "注释内容不能为空"
        await self._append_note(text)
        return True, "已记录注释"

    async def _append_note(self, text, entry=None, wall=None):
        """内部：追加操作行到 index.md（表格行格式）并实时推送到页面。

        wall 为事件发生时刻（DOM 事件取注入脚本回传的页面事件时间，
        见 _parse_op_time；缺省/None 用到达时刻——note/页面异常/回传
        失败的回落）。操作行序号 fr%08d 独立递增（与请求行 %010d 同
        规则，fr 前缀区分），同时写 {fr序号}.md 详情文件（事件字段表
        + 原始 JSON，列表点击"查看"打开）。文本清洗：\\r\\n\\t 与 |
        替换避免破坏表格。WS 广播为 type=record（与请求行统一格式），
        前端实时表格直接渲染操作行。entry 为 DOM 事件 dict
        （note/页面异常无）。"""
        if wall is None:
            wall = time.time()
        _, ts = self._fmt_wall(wall)   # 带日期（跨天可辨）
        text = _sanitize_op_text(text)
        self.op_counter += 1
        seq = "fr%08d" % self.op_counter
        self.notes.append((seq, wall, text))
        await self._append_to_index(self._op_index_row(seq, ts, text))
        await asyncio.to_thread(self._write_op_detail, seq, ts, text, entry)
        # 广播为 record 格式（is_op 标记），前端实时表格直接渲染
        await self._broadcast({"type": "record", "record": {
            "seq": seq, "time": ts, "method": "—", "type": "操作",
            "is_op": True, "url": text, "content_type": "",
            "req_size": 0, "resp_size": 0, "duration_ms": 0}})

    def _write_op_detail(self, seq, ts, text, entry=None):
        """操作行详情文件 {fr序号}.md：时间/描述/事件字段表 + 原始事件
        JSON（列表"查看"打开，原始展示不按请求/返回拆分）。"""
        lines = ["# 前端操作 %s\n\n" % seq,
                 "| 字段 | 值 |\n|---|---|\n",
                 "| 时间 | %s |\n" % ts,
                 "| 描述 | %s |\n" % text]
        if entry:
            lines.append("| 事件类型 | %s |\n" % _sanitize_op_text(
                entry.get("type", "")))
            lines.append("| 触发来源 | %s |\n" % (
                "人工" if entry.get("isTrusted", True) else "程序（合成）"))
            if entry.get("target"):
                lines.append("| 目标元素 | %s |\n" % _sanitize_op_text(
                    entry["target"]))
            for k, lab in (("value", "输入值"), ("checked", "勾选状态"),
                           ("key", "按键"), ("form", "表单"),
                           ("selection", "选中文本")):
                if entry.get(k) is not None:
                    lines.append("| %s | %s |\n" % (
                        lab, _sanitize_op_text(entry[k])))
            lines.append("\n原始事件：\n\n```\n%s\n```\n" % json.dumps(
                entry, ensure_ascii=False))
        with open(os.path.join(self.session_dir, seq + ".md"), "w",
                  encoding="utf-8") as f:
            f.writelines(lines)

    async def _on_console_op(self, session_id, params):
        """Runtime.consoleAPICalled：[OP] 前缀 → DOM 操作事件进 note 时间线。

        注入脚本 console.log('[OP] ' + JSON.stringify(entry)) 经 CDP
        Runtime.consoleAPICalled 事件回传（不受页面 CSP 约束），此处解析
        后格式化为文本写入时间线。不经 note() 的 source 校验——页面事件
        观察与抓包触发方式无关（人工/mcp 触发的会话同样记录）。
        """
        if not self.ops_inject or self.state not in (STATE_CAPTURING,
                                                     STATE_PAUSED):
            return
        if not self.session_dir:
            return
        args = params.get("args", [])
        if not args:
            return
        a = args[0]
        if a.get("type") != "string":
            return
        value = a.get("value", "")
        if not value.startswith("[OP] "):
            return
        try:
            entry = json.loads(value[5:])
        except (ValueError, TypeError):
            return
        entry_time = _parse_op_time(entry)
        # 历史操作事件过滤（问题记录 2026-09-28 问题 4）：Runtime.enable
        # 会重放页面 V8 控制台存储的历史消息（含此前会话/无会话期间
        # 遗留监听器产生的 [OP] 日志），事件时间早于会话开始的丢弃
        if (entry_time is not None and self.session_start_wall
                and entry_time < self.session_start_wall
                - _STALE_OP_TOLERANCE):
            self._stale_ops_dropped += 1
            return
        text = self._format_op_entry(entry)
        if text:
            await self._append_note(text, entry, wall=entry_time)

    async def _on_page_error(self, session_id, params):
        """Runtime.exceptionThrown：页面 JS 异常进 note 时间线（决策 8）。

        无需注入，服务端 CDP 直接接收；操作后页面报错与请求失败的
        关联分析有价值。
        """
        if not self.ops_inject or self.state not in (STATE_CAPTURING,
                                                     STATE_PAUSED):
            return
        if not self.session_dir:
            return
        details = params.get("exceptionDetails", {})
        text = (details.get("text") or
                (details.get("exception") or {}).get("description") or
                (details.get("exception") or {}).get("value") or "")
        if text:
            await self._append_note(
                "页面异常: %s" % str(text)[:120])

    @staticmethod
    def _format_op_entry(entry):
        """DOM 操作事件 dict → note 文本（格式化）。"""
        etype = entry.get("type", "")
        target = entry.get("target", "")
        parts = ["页面操作[%s] %s" % (etype, target)]
        if entry.get("value") is not None:
            parts.append(" value=%s" % entry["value"])
        if entry.get("checked") is not None:
            parts.append(" checked=%s" % entry["checked"])
        if entry.get("key"):
            parts.append(" key=%s" % entry["key"])
        if entry.get("form"):
            parts.append(" form=%s" % entry["form"])
        if entry.get("selection"):
            parts.append(" selection=%s" % entry["selection"])
        if not entry.get("isTrusted", True):
            parts.append(" (合成)")
        return "".join(parts)

    async def stop(self, source=None, reason="人工停止"):
        if self.state not in (STATE_CAPTURING, STATE_PAUSED):
            return False, "当前未在抓包"
        ok, err = self.check_source(source)
        if not ok:
            return False, err
        self.trigger = None
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
        # 抓包中逐条追加的 index 行序为到达顺序（响应完成顺序，并发时
        # 与请求发起顺序不同），结束时按时间排序重写一次
        await asyncio.to_thread(self._rewrite_index)
        # 拆除 DOM 操作监听注入：先取消等待文档提交的延迟注入任务，
        # 再逐 session 移除自动注入脚本并调用 __opCleanup 拆除当前
        # 文档中已安装的监听（避免残留监听在空闲期/下一会话继续回传）
        for task in self._inject_tasks.values():
            task.cancel()
        self._inject_tasks.clear()
        if self._injected and self.cdp and self.cdp.ws:
            for sid, identifier in self._injected.items():
                try:
                    await self.cdp.cmd(
                        "Page.removeScriptToEvaluateOnNewDocument",
                        {"identifier": identifier}, session_id=sid)
                except Exception as e:
                    log.warning("拆除操作监听失败: %s", e)
                try:
                    await self.cdp.cmd(
                        "Runtime.evaluate",
                        {"expression":
                         "window.__opCleanup&&window.__opCleanup()",
                         "returnByValue": True}, session_id=sid)
                except Exception:
                    pass  # 会话可能已失效，忽略
        self._injected.clear()
        # _tid_sid 与 _attached 保留（同一 CDP 连接的会话复用）：下一次
        # start 的 _attach_existing 会沿用 attach 并对已 attach 页面补
        # 注入；连接断开时 _on_cdp_close 统一清理
        self.ops_inject = False
        if self._stale_ops_dropped:
            log.info("已丢弃会话开始前的历史操作事件 %d 条（控制台缓冲"
                     "重放，见问题记录 2026-09-28 问题 4）",
                     self._stale_ops_dropped)
        mark_err = self._apply_mark_suffix()
        log.info("停止抓包(%s): %s", reason, self.session_name)
        await self._broadcast({"type": "state", **await asyncio.to_thread(self.status)})
        self._tray_notify("抓包已停止: %s" % self.session_name)
        if mark_err:
            await self._broadcast({
                "type": "mark_failed",
                "message": "标记重命名失败（%s），目录保持原名: %s" % (
                    mark_err, self.session_name)})
        return True, "抓包已停止: %s" % self.session_name

    def _index_header(self):
        return ("# 抓包汇总 %s\n\n" % self.session_name
                + "| 序号 | 请求时间 | 方法 | 资源类型 | 首次 | URL | 返回content-type | "
                  "请求body字节数 | 返回body字节数 | 耗时(ms) |\n"
                + "|---|---|---|---|---|---|---|---|---|---|\n")

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
            lines.append("%s: %s" % (k, _norm_header_value(v)))
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
                lines.append("%s: %s" % (k, _norm_header_value(v)))
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
            "time": datetime_str or time_str,
            "wall_time": rec.get("wall_time", 0),
            "method": req.get("method", ""),
            "type": rec.get("type") or "",
            "url": url,
            "content_type": self._header_of(resp_headers, "content-type"),
            "req_size": len(req_body_bytes),
            "resp_size": len(resp_body_bytes),
            "duration_ms": duration_ms,
        }
        self.records.append(row)
        await self._append_to_index(self._index_row(row))
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
        return True, "已删除 %d 条被过滤的记录（如静态资源），保留 %d 条" % (
            len(victims), len(self.records))

    def _rewrite_index(self):
        """全量重写 index.md：请求与操作注释合并为单一时间线，按时间排序。

        抓包中逐条追加的行序为到达顺序（响应完成顺序，并发请求时与发起
        顺序不同）；结束抓包时经此重写为按时间排序，操作→请求的因果链
        一目了然。"首次"列（★）标记每个接口（method + host + path，去
        query）按发起顺序的首次出现。单文件方案：抓包期间所有行直接写
        index.md（加锁），无临时文件。"""
        # 合并请求与操作行，按 wall_time 排序
        entries = []
        for row in self.records:
            entries.append((row.get("wall_time", 0), "req", row))
        for seq, wall, text in self.notes:
            entries.append((wall, "op", (seq, text)))
        entries.sort(key=lambda x: x[0])
        # 写表格
        seen = set()
        lines = [self._index_header()]
        for wall, kind, data in entries:
            if kind == "req":
                row = data
                key = _url_key(row)
                row["first_seen"] = key not in seen
                seen.add(key)
                lines.append(self._index_row(row))
            else:
                seq, text = data
                _, ts = self._fmt_wall(wall)
                lines.append(self._op_index_row(seq, ts, text))
        with open(os.path.join(self.session_dir, "index.md"), "w",
                  encoding="utf-8") as f:
            f.writelines(lines)

    @staticmethod
    def _index_row(row):
        return ("| %s | %s | %s | %s | %s | %s | %s | %s | %d | %d |\n" % (
            row["seq"], row["time"] or "-", row["method"],
            row["type"] or "-", "★" if row.get("first_seen") else "",
            row["url"], row["content_type"],
            row["req_size"], row["resp_size"], row["duration_ms"]))

    @staticmethod
    def _op_index_row(seq, ts, text):
        """操作行：与请求行同表格结构，资源类型列标"操作"。

        序号 fr%08d 独立递增（与请求行 %010d 同规则、前缀区分）；
        方法/首次/content-type/body字节数/耗时 均为占位值，URL 列放
        操作描述（如"页面操作[click] button \"查询\""），详情见
        {fr序号}.md 文件。"""
        return ("| %s | %s | — | 操作 | — | %s | — | 0 | 0 | 0 |\n" % (
            seq, ts, text))

    def _write_bytes(self, name, data):
        with open(os.path.join(self.session_dir, name), "wb") as f:
            f.write(data)

    async def _append_to_index(self, line):
        """向 index.md 追加一行（请求行与操作行统一入口，_index_lock 保护）。

        单文件方案：所有条目（后台接口请求与前端操作）统一写入 index.md，
        写入时加锁防止并发交错，时间精度到毫秒；结束抓包时 _rewrite_index
        从内存（records + notes）按时间排序全量重写，抓包期间文件中的行序
        为到达顺序（非排序），页面实时查看不受影响。"""
        async with self._index_lock:
            def _write():
                with open(os.path.join(self.session_dir, "index.md"), "a",
                          encoding="utf-8") as f:
                    f.write(line)
            await asyncio.to_thread(_write)

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
