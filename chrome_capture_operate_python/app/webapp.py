"""Web 层：FastAPI 路由（页面/REST/WebSocket/Cookie 接口）。

同端口通过 URI 区分：
- /                 Web 页面（六个 TAB）
- /api/...          REST 接口
- /ws/capture       WebSocket，实时推送抓包记录/状态/网址
"""
import asyncio
import logging
import os
import re
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse)
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

from . import chrome_proc
from . import globalconf
from .capture import (MARK_SUFFIX, STATE_IDLE, CaptureManager,
                      evaluate_violation, rename_with_retry)
from .config import (API_DOC_PATH, BASE_DIR, CAPTURE_DIR, EXTENSION_DIR,
                     PROJECT_ROOT, SCRIPTS_EXAMPLE_DIR, Config, ensure_dirs,
                     get_auto_start)
from .cookie_store import CookieStore
from .executor import Executor, list_scripts, script_roots
from .scheduler import Scheduler, describe_schedule

log = logging.getLogger("app.web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "static")

# 搜索范围 key -> 展示名（请求/返回 × 头/body）
SCOPE_LABELS = {"req_header": "请求头", "req_body": "请求body",
                "resp_header": "返回头", "resp_body": "返回body"}


def _under_script_roots(p):
    """路径是否位于任一脚本根目录（示例/用户自定义）内。"""
    return any(p.startswith(r + os.sep) for r in script_roots())


def _split_md(content):
    """{seq}.md -> (req_part, resp_part)，各自以 # request/# response 开头。"""
    idx = content.find("\n# response")
    if idx < 0:
        return content, ""
    return content[:idx], content[idx + 1:]


def _strip_meta(part):
    """去掉首行 # request/# response 标记与其后的 [...] 元信息行。"""
    lines = part.split("\n")
    i = 0
    if i < len(lines) and lines[i].startswith("#"):
        i += 1
    while i < len(lines) and lines[i].startswith("["):
        i += 1
    return "\n".join(lines[i:])


def _md_part(content, key):
    """取 {seq}.md 的指定部分（头含请求行/状态行；头与 body 以空行分隔）。"""
    req, resp = _split_md(content)
    text = _strip_meta(req if key.startswith("req") else resp)
    if "\n\n" in text:
        head, body = text.split("\n\n", 1)
    else:
        head, body = text, ""
    return head if key.endswith("header") else body


def _search_md_lines(lines, kw, scopes):
    """按行单遍扫描 {seq}.md，返回命中的范围 key 列表（中文/任意子串均可，
    大小写不敏感）。性能要点：一行命中即记录该范围并停止该范围后续行扫描，
    全部选中范围命中后立即返回（早停）——不拼接整段文本、不重复 lower。"""
    hit = set()
    section = None   # 当前处于 req / resp
    in_body = False  # 空行之后即 body
    for line in lines:
        stripped = line.strip()
        if stripped == "# request":
            section, in_body = "req", False
            continue
        if stripped == "# response":
            section, in_body = "resp", False
            continue
        if section is None:
            continue  # 文件头部的杂项行
        if stripped.startswith("[") and not in_body:
            continue  # 元信息行（[请求时间] 等），不属于头也不属于 body
        if not in_body and not stripped:
            in_body = True  # 头与 body 之间的空行
            continue
        key = section + ("_body" if in_body else "_header")
        if key in scopes and key not in hit and kw in line.lower():
            hit.add(key)
            if len(hit) == len(scopes):
                break  # 选中范围全部命中，无需再扫
    return [k for k in SCOPE_LABELS if k in hit]


class WSClients:
    def __init__(self):
        self.clients = set()

    async def broadcast(self, payload):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def create_app():
    ensure_dirs()
    config = Config()
    store = CookieStore()
    capture = CaptureManager(config)
    executor = Executor(config)
    scheduler = Scheduler(config)
    ws_clients = WSClients()
    capture.on_event = ws_clients.broadcast

    @asynccontextmanager
    async def lifespan(app):
        # 定时执行脚本的调度循环（uvicorn 启动时开启，退出时停止）
        loop_task = asyncio.create_task(scheduler.run_loop())
        yield
        loop_task.cancel()

    app = FastAPI(title="chrome_capture_operate", lifespan=lifespan)

    # ---------------- 页面 ----------------
    @app.get("/", response_class=HTMLResponse)
    async def index():
        with open(os.path.join(STATIC_DIR, "index.html"), "r",
                  encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # ---------------- 状态 ----------------
    @app.get("/api/extension-dir")
    async def extension_dir():
        """Chrome 插件目录路径（页头"安装Chrome插件"按钮复制给用户加载）。"""
        return {"path": EXTENSION_DIR}

    # 使用说明中的截图（项目根 pics/ 目录）：/pics/<文件名> 直接访问，
    # usage.md 中以 ![说明](pics/xxx.png) 引用
    _pics_dir = os.path.join(PROJECT_ROOT, "pics")
    if os.path.isdir(_pics_dir):
        app.mount("/pics", StaticFiles(directory=_pics_dir), name="pics")

    @app.get("/health")
    async def health():
        """健康检查：检测运行状态并返回当前时间（供验证脚本访问）。"""
        return {"ok": True, "status": "running",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    @app.get("/api/status")
    async def status():
        port = config.get("port")
        # chrome_alive 探测（缓存未命中时是同步 HTTP，可能阻塞数秒）放
        # 后台线程，避免卡住事件循环拖慢所有接口
        base = await asyncio.to_thread(capture.status)
        return {
            **base,
            "port": port,
            "cdp_port": config.get("cdp_port"),
            "cookie_total": store.total(),
            "last_push_time": store.last_push_time,
        }

    # ---------------- Chrome 进程 ----------------
    @app.post("/api/chrome/start")
    async def chrome_start():
        ok, msg = await asyncio.to_thread(
            chrome_proc.start_capture_chrome, config.get("cdp_port"))
        if ok:
            try:
                await capture.ensure_cdp()
            except Exception as e:
                log.warning("CDP 连接失败: %s", e)
        return {"ok": ok, "message": msg}

    # ---------------- Chrome 进程多开 ----------------
    # 实例信息存全局配置 chrome_multi_profile：[{id, name, dir, created}]。
    # id 即"实例编号"（1 起自增），插件设置页下拉选择它对号——扩展无 API
    # 获取 user-data-dir，人工选择一次（与"每实例分别安装插件"合并成一步）。

    def _multi_profiles():
        return globalconf.get_json(globalconf.KEY_MULTI_PROFILE, [])

    class MultiProfileBody(BaseModel):
        name: str
        dir: str

    @app.get("/api/multi-profiles")
    async def multi_profiles_list():
        """多开实例列表（插件设置页下拉对号也拉此列表）。"""
        return {"profiles": _multi_profiles()}

    @app.post("/api/multi-profiles")
    async def multi_profiles_add(body: MultiProfileBody):
        name = body.name.strip()
        d = body.dir.strip()
        if not name:
            return JSONResponse({"error": "名称不能为空"}, status_code=400)
        if not d or not os.path.isabs(d):
            return JSONResponse({"error": "请输入存在的本地目录完整路径"},
                                status_code=400)
        if not os.path.isdir(d):
            return JSONResponse({"error": "目录不存在：%s" % d},
                                status_code=400)
        items = _multi_profiles()
        if any(it.get("name") == name for it in items):
            return JSONResponse({"error": "名称已存在：%s" % name},
                                status_code=400)
        # 编号 = 现有最大 id + 1（删除后不复用，保持稳定）
        next_id = max((it.get("id", 0) for it in items), default=0) + 1
        item = {"id": next_id, "name": name, "dir": d,
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        items.append(item)
        if not globalconf.set_json(globalconf.KEY_MULTI_PROFILE, items):
            return JSONResponse({"error": "全局配置写入失败"},
                                status_code=500)
        log.info("新增多开实例: 编号%d 名称=%s 目录=%s",
                 next_id, name, d)
        return {"ok": True, "profile": item}

    @app.post("/api/multi-profiles/{profile_id}/open")
    async def multi_profiles_open(profile_id: int):
        items = _multi_profiles()
        item = next((it for it in items if it.get("id") == profile_id), None)
        if not item:
            return JSONResponse({"error": "实例不存在"}, status_code=404)
        # 首次打开前拷贝收藏夹/历史/用户名（增量，重复执行代价小）
        await asyncio.to_thread(chrome_proc.copy_profile_to, item["dir"])
        # 不再生成"变体扩展"：FORCED_PROFILE 注入方案已回退为插件
        # "参数配置"页人工设置编号（见 docs/design-profile-id.md），注入的
        # 常量在新版 background.js 中已不被读取，生成它只会误导用户
        ok, msg = await asyncio.to_thread(
            chrome_proc.start_chrome_with_profile, item["dir"])
        return {"ok": ok, "message": msg}

    @app.post("/api/multi-profiles/{profile_id}/delete")
    async def multi_profiles_delete(profile_id: int):
        items = _multi_profiles()
        rest = [it for it in items if it.get("id") != profile_id]
        if len(rest) == len(items):
            return JSONResponse({"error": "实例不存在"}, status_code=404)
        globalconf.set_json(globalconf.KEY_MULTI_PROFILE, rest)
        log.info("删除多开实例: 编号%d", profile_id)
        return {"ok": True}


    # ---------------- 抓包 ----------------
    @app.get("/api/capture/config")
    async def capture_get_conf():
        return capture.capture_conf

    class CaptureConf(BaseModel):
        suffix_enabled: bool = True
        content_type_enabled: bool = True
        type_enabled: bool = True
        domains: list = []
        uri_rules: list = []
        ws_capture: bool = False

    @app.put("/api/capture/config")
    async def capture_put_conf(conf: CaptureConf):
        # 会话级配置，不写 conf.json；抓包中调整对后续数据即时生效
        capture.capture_conf.update(conf.dict())
        return {"ok": True}

    @app.get("/api/capture/hosts")
    async def capture_hosts():
        return {"hosts": sorted(capture.seen_hosts)}

    @app.post("/api/capture/start")
    async def capture_start():
        ok, msg = await capture.start()
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/pause")
    async def capture_pause():
        ok, msg = await capture.pause()
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/resume")
    async def capture_resume():
        ok, msg = await capture.resume()
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/stop")
    async def capture_stop():
        ok, msg = await capture.stop()
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/purge")
    async def capture_purge():
        ok, msg = await capture.purge()
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/mark")
    async def capture_mark():
        ok, msg = await capture.mark()
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.websocket("/ws/capture")
    async def ws_capture(ws: WebSocket):
        await ws.accept()
        ws_clients.clients.add(ws)
        try:
            # 初始状态与 /api/status 同口径（含 cookie_total），否则前端
            # 顶栏 Cookie 计数在 WS 首包时显示 undefined；探测走线程（同 /api/status）
            init_state = await asyncio.to_thread(capture.status)
            await ws.send_json({"type": "state", **init_state,
                                "cookie_total": store.total()})
            # 补发当前会话已有记录：页面 F5 刷新后表格能恢复展示
            # （已结束的会话不补发——结束抓包即清空展示）
            if capture.state != STATE_IDLE and capture.records:
                await ws.send_json({"type": "records",
                                    "records": capture.records})
            while True:
                await ws.receive_text()  # 仅需保持连接
        except WebSocketDisconnect:
            pass
        finally:
            ws_clients.clients.discard(ws)

    # ---------------- 抓包记录 ----------------
    def _session_path(name):
        """目录名合法性：不含路径分隔符、非 . / ..，且解析后位于抓包根目录内。"""
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            return None
        root = os.path.abspath(CAPTURE_DIR)
        p = os.path.abspath(os.path.join(root, name))
        if not p.startswith(root + os.sep):
            return None
        return p if os.path.isdir(p) else None

    @app.get("/api/history")
    async def history():
        # 只统计记录数（按文件名匹配，不做 getsize——stat 在 Windows 下很慢）；
        # 放后台线程避免阻塞事件循环
        def _collect():
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
                              "path": os.path.join(CAPTURE_DIR, d)})
            return items

        items = await asyncio.to_thread(_collect)
        return {"sessions": [i["name"] for i in items], "items": items,
                "active": capture.session_name
                if capture.state == "capturing" else None}

    @app.get("/api/history/{session}/files")
    async def history_files(session: str):
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        files = sorted(os.listdir(p))
        return {"files": files}

    def _parse_index_records(index_path):
        """解析 index.md 汇总表行为结构化记录（跳过表头/分隔行/末尾说明）。"""
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
                    if len(cells) != 9:
                        continue
                    try:
                        records.append({
                            "seq": cells[0],
                            "time": "" if cells[1] == "-" else cells[1],
                            "method": cells[2],
                            "type": "" if cells[3] == "-" else cells[3],
                            "url": cells[4],
                            "content_type": cells[5],
                            "req_size": int(cells[6]),
                            "resp_size": int(cells[7]),
                            "duration_ms": int(cells[8]),
                        })
                    except (ValueError, IndexError):
                        continue
        except OSError:
            pass
        return records

    @app.get("/api/history/{session}/records")
    async def history_records(session: str):
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        return {"records": _parse_index_records(
            os.path.join(p, "index.md"))}

    @app.delete("/api/history/{session}")
    async def history_delete(session: str):
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        if (capture.state == "capturing"
                and session == capture.session_name):
            return JSONResponse({"error": "正在抓包中的目录不允许删除"},
                                status_code=400)
        ok, err = await asyncio.to_thread(_rmtree_retry, p)
        if ok:
            return {"ok": True}
        return JSONResponse({"error": "删除失败: %s" % err},
                            status_code=500)

    class BatchDeleteBody(BaseModel):
        sessions: list

    @app.post("/api/history/batch_delete")
    async def history_batch_delete(body: BatchDeleteBody):
        deleted, failed = [], []
        for name in body.sessions:
            p = _session_path(name)
            if not p:
                failed.append(name)
                continue
            if (capture.state == "capturing"
                    and name == capture.session_name):
                failed.append(name)  # 正在抓包中的目录不允许删除
                continue
            ok, _ = await asyncio.to_thread(_rmtree_retry, p)
            (deleted if ok else failed).append(name)
        return {"ok": not failed,
                "deleted": deleted, "deleted_count": len(deleted),
                "failed": failed, "failed_count": len(failed)}

    def _rmtree_retry(path, attempts=3, delay=0.3):
        """删除目录（带重试）：杀毒/索引器瞬态锁会导致 WinError 5。"""
        import shutil
        import time
        err = None
        for i in range(attempts):
            try:
                shutil.rmtree(path)
                return True, None
            except OSError as e:
                err = e
                if i < attempts - 1:
                    time.sleep(delay)
        return False, err

    @app.get("/api/history/{session}/file")
    async def history_file(session: str, name: str = Query(...)):
        p = _session_path(session)
        if not p or not re.fullmatch(r"[0-9A-Za-z_.\-]{1,64}", name):
            return JSONResponse({"error": "参数非法"}, status_code=400)
        fp = os.path.join(p, name)
        if not os.path.isfile(fp):
            return JSONResponse({"error": "文件不存在"}, status_code=404)
        if name.endswith(".md"):
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                return {"name": name, "content": f.read()}
        return {"name": name, "binary": True,
                "size": os.path.getsize(fp),
                "content": "<二进制文件，不支持预览>"}

    # ---------------- 搜索内容（抓包页/抓包记录页共用） ----------------
    class SearchBody(BaseModel):
        keyword: str = ""
        scopes: list = ["req_header", "req_body", "resp_header", "resp_body"]

    @app.post("/api/history/{session}/search")
    async def history_search(session: str, body: SearchBody):
        """在 {seq}.md 的请求/返回 × 头/body 中搜索关键字（大小写不敏感）。
        抓包页传当前会话名即可搜索进行中的会话。"""
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        keyword = (body.keyword or "").strip()
        if not keyword:
            return JSONResponse({"error": "关键字不能为空"}, status_code=400)
        scopes = [s for s in body.scopes if s in SCOPE_LABELS]
        if not scopes:
            return JSONResponse({"error": "请选择搜索范围"}, status_code=400)

        def _search():
            records = {r["seq"]: r for r in _parse_index_records(
                os.path.join(p, "index.md"))}
            matches, searched = [], 0
            kw = keyword.lower()
            try:
                names = sorted(os.listdir(p))
            except OSError:
                names = []
            for f in names:
                if not re.fullmatch(r"[0-9]{10}\.md", f):
                    continue
                searched += 1
                try:
                    with open(os.path.join(p, f), "r", encoding="utf-8",
                              errors="replace") as fh:
                        # 按行流式扫描：单遍、命中即早停，避免大文件
                        # 整体读入后再多次切分/拼接/lower
                        parts = _search_md_lines(fh, kw, scopes)
                except OSError:
                    continue
                if parts:
                    row = dict(records.get(f[:-3], {}))
                    row["seq"] = f[:-3]
                    row["parts"] = [SCOPE_LABELS[k] for k in parts]
                    matches.append(row)
            return matches, searched

        matches, searched = await asyncio.to_thread(_search)
        return {"matches": matches, "searched": searched,
                "match_count": len(matches)}

    # ---------------- 删除不满足条件的记录（抓包记录页） ----------------
    class HistoryPurgeBody(BaseModel):
        """与抓包页面的抓包配置同构（会话级，仅本次删除生效）。"""
        suffix_enabled: bool = True
        content_type_enabled: bool = True
        type_enabled: bool = True
        domains: list = []
        uri_rules: list = []

    @app.post("/api/history/{session}/purge")
    async def history_purge(session: str, body: HistoryPurgeBody):
        """按给定过滤条件删除历史会话中不满足的记录：删文件、重写 index.md，
        序号保持不变。评估口径与抓包中 purge 完全一致。"""
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        if (capture.state == "capturing"
                and session == capture.session_name):
            return JSONResponse({"error": "正在抓包中的目录不允许删除记录"},
                                status_code=400)

        def _do():
            records = _parse_index_records(os.path.join(p, "index.md"))
            conf = body.dict()
            victims = [r for r in records
                       if evaluate_violation(r, conf, config)]
            keep = [r for r in records
                    if not evaluate_violation(r, conf, config)]
            # 删除记录文件（.md 及同名二进制文件）
            for r in victims:
                seq = r["seq"]
                try:
                    for f in os.listdir(p):
                        if f.startswith(seq + "."):
                            try:
                                os.remove(os.path.join(p, f))
                            except OSError as e:
                                log.warning("删除文件失败 %s: %s", f, e)
                except OSError:
                    pass
            # 重写 index.md（与抓包汇总相同格式，序号不重排）
            lines = ["# 抓包汇总 %s\n\n" % session
                     + "| 序号 | 请求时间 | 方法 | 资源类型 | URL | 返回content-type | 请求body字节数 | "
                       "返回body字节数 | 耗时(ms) |\n"
                     + "|---|---|---|---|---|---|---|---|---|\n"]
            for row in keep:
                lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |\n" % (
                    row["seq"], row.get("time") or "-",
                    row.get("method", ""), row.get("type") or "-",
                    row.get("url", ""), row.get("content_type", ""),
                    row.get("req_size", 0), row.get("resp_size", 0),
                    row.get("duration_ms", 0)))
            with open(os.path.join(p, "index.md"), "w",
                      encoding="utf-8") as f:
                f.writelines(lines)
            return len(victims), len(keep)

        deleted, kept = await asyncio.to_thread(_do)
        log.info("历史会话删除不满足条件的记录(%s): 删 %d 留 %d",
                 session, deleted, kept)
        return {"ok": True, "deleted": deleted, "kept": kept,
                "message": "已删除 %d 条不满足条件的记录，保留 %d 条" % (
                    deleted, kept)}

    class MarkBody(BaseModel):
        marked: bool

    @app.put("/api/history/{session}/mark")
    async def history_mark(session: str, body: MarkBody):
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        if capture.state == "capturing" and session == capture.session_name:
            return JSONResponse({"error": "只能对已经结束的操作标记"},
                                status_code=400)
        has_suffix = session.endswith(MARK_SUFFIX)
        if body.marked == has_suffix:
            return {"ok": True, "name": session, "changed": False}
        new_name = (session + MARK_SUFFIX if body.marked
                    else session[:-len(MARK_SUFFIX)])
        ok, err = rename_with_retry(
            p, os.path.join(CAPTURE_DIR, new_name))
        if ok:
            return {"ok": True, "name": new_name, "changed": True}
        return JSONResponse({"error": "重命名失败: %s" % err},
                            status_code=500)

    class RenameBody(BaseModel):
        name: str = ""

    @app.put("/api/history/{session}/rename")
    async def history_rename(session: str, body: RenameBody):
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        if (capture.state == "capturing"
                and session == capture.session_name):
            return JSONResponse(
                {"error": "正在抓包中的子目录不允许重命名"}, status_code=400)
        # 编辑框展示完整目录名，按输入的整体新名重命名
        new_name = (body.name or "").strip()
        if not new_name:
            return JSONResponse({"error": "名称不能为空"}, status_code=400)
        if new_name == session:
            return {"ok": True, "name": session, "changed": False}
        if any(c in new_name
               for c in ('/', '\\', '<', '>', ':', '"', '|', '?', '*')):
            return JSONResponse({"error": "名称包含非法字符"}, status_code=400)
        if new_name in (".", ".."):
            return JSONResponse({"error": "非法名称"}, status_code=400)
        if len(new_name) > 80:
            return JSONResponse({"error": "名称过长（最多80字符）"},
                                status_code=400)
        if _session_path(new_name):
            return JSONResponse({"error": "已存在同名目录"}, status_code=400)
        ok, err = rename_with_retry(p, os.path.join(CAPTURE_DIR, new_name))
        if not ok:
            return JSONResponse({"error": "重命名失败: %s" % err},
                                status_code=500)
        return {"ok": True, "name": new_name, "changed": True}

    @app.post("/api/history/{session}/prompt")
    async def history_prompt(session: str):
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        # 生成前必须已配置固化脚本保存根目录（全局配置
        # python_scripts_dir_path，在快速执行脚本页面配置）
        scripts_root = (globalconf.get_value(
            globalconf.KEY_SCRIPTS_DIR) or "").strip()
        if not scripts_root:
            return JSONResponse(
                {"error": "尚未配置保存生成的固化Python脚本文件根目录，"
                          "请先在快速执行脚本页面配置"},
                status_code=400)
        # 结构化提示词模板（对应 prompt.md“生成提示词”节）；
        # 两个“需要人工填写”节由人工在弹窗中编辑后复制给 AI；
        # 路径前后带空格，便于 AI 识别路径边界
        text = (
            "# 提供数据\n"
            "%s 为浏览器访问抓包记录目录，需要根据文件内容进行分析，"
            "首先读取index.md汇总文件，再按序号查看明细文件\n"
            "%s 需要根据文件内容了解Python脚本生成的执行环境与依赖约束等\n"
            "# 生成数据\n"
            "需要在 %s 下生成一个子目录，子目录名需要根据内容生成一个合适的名称，"
            "可使用中文或英文，在该子目录中生成以下文件\n"
            "## Python脚本\n"
            "请求中使用的数据需要来自用户指定的值，或者通过网站提供的查询接口获取，"
            "尽量不要硬编码，假如存在参数值无法确定来源，需要提醒人工确认，"
            "可能是因为抓包内容缺少了获取对应数据的请求\n"
            "具体要求见后续描述\n"
            "## README.md\n"
            "说明当前脚本的作用、使用说明等\n"
            "## prompt.md\n"
            "记录本次使用的完整提示词（含人工补充的操作描述与具体要求）\n"
            "# 人工在网页的操作描述\n"
            "（需要人工填写：说明当前进行了什么操作，有哪些页面有展示的重要的值是什么）\n"
            "# 生成Python脚本具体要求\n"
            "（需要人工填写：说明生成的Python脚本需要执行什么功能，是否有入参，执行逻辑是什么）\n"
        ) % (os.path.abspath(p), os.path.abspath(API_DOC_PATH),
             os.path.abspath(scripts_root))
        return {"prompt": text}

    # ---------------- Cookie ----------------
    class PushBody(BaseModel):
        reason: str = "未知"
        cookies: list = []
        profile: int = 0   # Chrome profile（多开实例）编号，未设置为 0

    @app.post("/api/cookies/push")
    async def cookies_push(body: PushBody, request: Request):
        source = request.client.host if request.client else ""
        ok, count = store.receive(body.cookies, body.reason, source,
                                   body.profile)
        if ok:
            # 记录目标服务器地址（cookie域名）、数量与 key（只记名，值不落
            # 日志——脱敏要求）；保留来源地址便于定位多浏览器互相覆盖问题
            valid = [c for c in body.cookies
                     if isinstance(c, dict) and c.get("name")]
            domains = ",".join(sorted({str(c.get("domain", "")) for c in valid
                                       if c.get("domain")}))
            keys = ",".join(sorted({str(c.get("name", "")) for c in valid}))
            log.info("收到 Cookie 推送: 地址=%s profile=%d 目标服务器=%s "
                     "reason=%s 数量=%d key: %s",
                     source, body.profile, domains or "无", body.reason,
                     count, keys or "无")
            # 数量为 0 时广播红色提醒（prompt.md 接收Cookie功能要求）：
            # 可能是插件未设置推送Cookie范围（默认全部禁止）
            if count == 0:
                await ws_clients.broadcast({
                    "type": "cookie_push_empty",
                    "profile": body.profile,
                })
                log.warning("接收到的Chrome Cookie数量为 0（profile=%d），"
                            "可能是在Chrome插件中未设置推送Cookie范围",
                            body.profile)
        else:
            log.info("收到 Cookie 推送失败: 地址=%s reason=%s", source, body.reason)
        return {"ok": ok, "count": count}

    @app.get("/api/cookies/query")
    async def cookies_query(url: str = Query(...),
                            profile: int = Query(0)):
        """profile 为 0（默认）时查所有 Chrome profile；非 0 查指定编号。"""
        cookies, err = store.query(url, profile)
        if err:
            log.info("获取cookie请求 url=%s profile=%d 失败: %s",
                     url, profile, err)
            return JSONResponse({"ok": False, "error": err}, status_code=404)
        # 只记录数量与 key，不记录 cookie 值（日志脱敏要求）
        keys = ",".join(c.get("name", "") for c in cookies)
        log.info("获取cookie请求 url=%s profile=%d 返回 %d 条 cookie（key: %s）",
                 url, profile, len(cookies), keys or "无")
        return {"ok": True,
                "cookies": cookies,
                "cookie_header": CookieStore.cookie_header(cookies)}

    @app.get("/api/cookies/receives")
    async def cookies_receives():
        return {"receives": store.receives(), "total": store.total(),
                "last_push_time": store.last_push_time}

    # ---------------- 使用说明 ----------------
    @app.get("/api/usage")
    async def usage():
        """使用说明内容（chrome_capture_operate_python/usage/usage.md，
        前端按 markdown 渲染）。"""
        path = os.path.join(BASE_DIR, "usage", "usage.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {"content": f.read()}
        except OSError as e:
            return {"error": "读取使用说明失败: %s" % e}

    # ---------------- 全局配置 ----------------
    @app.get("/api/global_conf")
    async def global_conf_get(key: str = Query(...)):
        return {"key": key, "value": globalconf.get_value(key)}

    class GlobalConfBody(BaseModel):
        key: str
        value: str = ""

    @app.put("/api/global_conf")
    async def global_conf_put(body: GlobalConfBody):
        value = (body.value or "").strip()
        # 固化脚本保存根目录：必须是存在的合法目录路径
        if body.key == globalconf.KEY_SCRIPTS_DIR:
            if not value or not os.path.isdir(value):
                return JSONResponse(
                    {"error": "指定的目录不存在，请填写存在的合法目录路径"},
                    status_code=400)
            value = os.path.abspath(value)
        if not globalconf.set_value(body.key, value):
            return JSONResponse({"error": "写入全局配置文件失败"},
                                status_code=500)
        return {"ok": True, "key": body.key, "value": value}

    # ---------------- 快速执行 ----------------
    @app.get("/api/scripts")
    async def scripts():
        return {"scripts": list_scripts(),
                "example_root": os.path.abspath(SCRIPTS_EXAMPLE_DIR),
                "user_root": (globalconf.get_value(
                    globalconf.KEY_SCRIPTS_DIR) or "")}

    class RunBody(BaseModel):
        path: str

    @app.get("/api/scripts/file")
    async def scripts_file(path: str = Query(...)):
        p = os.path.abspath(path)
        if not _under_script_roots(p) or not os.path.isfile(p):
            return JSONResponse({"error": "文件不存在"}, status_code=404)
        ext = os.path.splitext(p)[1].lower()
        if ext not in (".py", ".md"):
            return JSONResponse({"error": "仅支持查看 .py/.md 文件"},
                                status_code=400)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return {"name": os.path.basename(p), "path": p,
                    "ext": ext, "content": f.read()}

    @app.post("/api/scripts/run")
    async def scripts_run(body: RunBody):
        ex = await executor.start(body.path)
        if not ex:
            return JSONResponse(
                {"error": "脚本不存在或不在允许的脚本目录内"},
                status_code=400)
        return {"exec_id": ex.id}

    @app.delete("/api/scripts/dir")
    async def scripts_delete_dir(path: str = Query(...)):
        """删除脚本根目录（示例/用户自定义）下的直接子目录。"""
        p = os.path.abspath(path)
        if (not any(p.startswith(r + os.sep) and os.path.dirname(p) == r
                    for r in script_roots())
                or not os.path.isdir(p)):
            return JSONResponse(
                {"error": "目录不存在或不是脚本根目录下的子目录"},
                status_code=404)
        ok, err = await asyncio.to_thread(_rmtree_retry, p)
        if not ok:
            return JSONResponse({"error": "删除失败: %s" % err},
                                status_code=500)
        log.info("已删除脚本子目录: %s", p)
        return {"ok": True}

    @app.get("/api/exec/{exec_id}")
    async def exec_status(exec_id: str):
        ex = executor.get(exec_id)
        if not ex:
            return JSONResponse({"error": "执行不存在"}, status_code=404)
        return ex.to_dict()

    @app.post("/api/exec/{exec_id}/kill")
    async def exec_kill(exec_id: str):
        ok = await executor.kill(exec_id)
        return {"ok": ok}

    # ---------------- 定时执行脚本 ----------------
    @app.get("/api/schedules")
    async def schedules_list():
        items = scheduler.list_tasks()
        for t in items:
            t["schedule_desc"] = describe_schedule(t.get("schedule") or {})
        return {"schedules": items}

    class ScheduleBody(BaseModel):
        script_path: str
        schedule: dict

    @app.post("/api/schedules")
    async def schedules_add(body: ScheduleBody):
        task, err = scheduler.add(body.script_path, body.schedule)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True, "task": task}

    @app.put("/api/schedules/{sid}")
    async def schedules_update(sid: str, body: ScheduleBody):
        task, err = scheduler.update(sid, body.script_path, body.schedule)
        if err:
            return JSONResponse({"error": err},
                                status_code=404 if err == "任务不存在"
                                else 400)
        return {"ok": True, "task": task}

    @app.delete("/api/schedules/{sid}")
    async def schedules_delete(sid: str):
        if not scheduler.delete(sid):
            return JSONResponse({"error": "任务不存在"}, status_code=404)
        return {"ok": True}

    @app.post("/api/schedules/{sid}/pause")
    async def schedules_pause(sid: str):
        task, err = scheduler.pause(sid)
        if err:
            return JSONResponse({"error": err}, status_code=404)
        return {"ok": True, "task": task}

    @app.post("/api/schedules/{sid}/resume")
    async def schedules_resume(sid: str):
        task, err = scheduler.resume(sid)
        if err:
            return JSONResponse({"error": err}, status_code=404)
        return {"ok": True, "task": task}

    @app.get("/api/schedules/{sid}/logs")
    async def schedules_logs(sid: str, date: str = Query("")):
        """某任务的执行日志列表（date=YYYY-MM-DD 可选过滤）。"""
        with scheduler._lock:
            task = scheduler.tasks.get(sid)
        if not task:
            return JSONResponse({"error": "任务不存在"}, status_code=404)
        subdir = os.path.basename(
            os.path.dirname(os.path.abspath(task["script_path"])))
        date = (date or "").strip()
        if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return JSONResponse({"error": "日期格式需为 YYYY-MM-DD"},
                                status_code=400)
        logs = await asyncio.to_thread(scheduler.list_logs, subdir, date)
        return {"subdir": subdir, "logs": logs}

    @app.get("/api/schedules/log_file")
    async def schedules_log_file(path: str = Query(...)):
        """读取执行日志内容（仅限定时执行日志目录内）。"""
        p = os.path.abspath(path)
        log_root = os.path.abspath(scheduler.log_root)
        if (not p.startswith(log_root + os.sep)
                or not os.path.isfile(p)):
            return JSONResponse({"error": "日志文件不存在"}, status_code=404)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return {"name": os.path.basename(p), "path": p, "content": f.read()}

    # ---------------- 参数配置 ----------------
    @app.get("/api/config")
    async def get_conf():
        d = config.as_dict()
        d["auto_start"] = get_auto_start()
        return d

    class ConfBody(BaseModel):
        port: int = None
        auto_start: bool = None
        suffix_filter: list = None
        content_type_filter: list = None
        type_filter: list = None
        cdp_port: int = None
        exec_timeout_sec: int = None

    @app.put("/api/config")
    async def put_conf(body: ConfBody):
        updates = {k: v for k, v in body.dict().items() if v is not None}
        res = config.update(**updates)
        note = ("监听端口与 CDP 端口修改后需重启 Python 脚本生效；"
                "修改监听端口后需同步修改 Chrome 插件 "
                "chrome_capture_operate_extension 中的推送目标地址")
        if res and not res.get("auto_start_registry_ok"):
            note = ("警告：开机自启动注册表写入失败，参数已保存，"
                    "重启程序后将自动重试。" + note)
        return {"ok": True, "note": note}

    @app.get("/api/api_md_path")
    async def api_md_path():
        return {"path": os.path.abspath(API_DOC_PATH),
                "project_root": PROJECT_ROOT}

    return app
