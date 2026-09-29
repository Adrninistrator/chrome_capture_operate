"""Web 层：FastAPI 路由（页面/REST/WebSocket/Cookie 接口）。

同端口通过 URI 区分：
- /                 Web 页面（六个 TAB）
- /api/...          REST 接口
- /ws/capture       WebSocket，实时推送抓包记录/状态/网址
"""
import asyncio
import json
import logging
import os
from urllib.parse import urlsplit
import re
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse)
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

from . import chrome_proc
from . import cookie_cdp
from . import cookie_query_log
from . import globalconf
from . import mcpserver
from .capture import (MARK_SUFFIX, STATE_IDLE, CaptureManager,
                      evaluate_violation, list_history_sessions,
                      parse_index_records, purge_history_session,
                      rename_with_retry, session_path)
from .config import (ANALYSIS_GUIDE_PATH, API_DOC_PATH, BASE_DIR, EXTENSION_DIR,
                     LOG_DIR, PROJECT_ROOT, SCRIPTS_EXAMPLE_DIR, Config,
                     ensure_dirs, get_auto_start)
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


# ---------------- AI自主操作Chrome：Claude Code 项目 MCP 一键配置 ----------------
# 页头"AI自主操作Chrome" → b. AI Agent MCP配置 → 使用Claude Code：指定项目
# 目录后点击"配置"按钮，在该目录写入两个文件（不存在则创建，已存在则增
# 加缺失配置、同名key的value覆盖更新，无关配置不动）：
#   .mcp.json                    mcpServers：chrome-devtools-attach /
#                                chrome-operate
#   .claude\settings.local.json  permissions.allow、enableAllProjectMcpServers、
#                                enabledMcpjsonServers

def _load_json_obj(path):
    """读取 JSON 配置文件为 dict。

    文件不存在返回 ({}, None)——按"不存在则创建"处理；读取失败/非 JSON
    对象返回 (None, 错误)——不覆盖损坏文件，交人工处理。
    """
    if not os.path.isfile(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, "%s 内容不是有效JSON：%s" % (os.path.basename(path), e)
    if not isinstance(data, dict):
        return None, "%s 内容不是JSON对象" % os.path.basename(path)
    return data, None


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def merge_claude_code_conf(project_dir, cdp_port, port):
    r"""写入 Claude Code 项目的 MCP 配置（.mcp.json + .claude\settings.local.json）。

    文件不存在则创建；已存在则增加缺失配置，同名 key 的 value 以本次
    配置覆盖更新（如端口变更后重跑即刷新旧值）；无关的其他配置保留
    不动。全部一致时不写文件（保留原格式）。返回 (完成信息, 错误)。
    """
    servers = {
        "chrome-devtools-attach": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest",
                     "--browser-url=http://127.0.0.1:%s" % cdp_port],
            "env": {},
        },
        "chrome-operate": {
            "type": "sse",
            "url": "http://127.0.0.1:%s/mcp/sse" % port,
        },
    }
    allow_rules = ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)",
                   "WebSearch(*)", "WebFetch(*)",
                   "mcp__chrome-devtools-attach__*", "mcp__chrome-operate__*"]

    mcp_path = os.path.join(project_dir, ".mcp.json")
    data, err = _load_json_obj(mcp_path)
    if err:
        return None, err
    mcp = data.setdefault("mcpServers", {})
    if not isinstance(mcp, dict):
        return None, ".mcp.json 的 mcpServers 不是JSON对象"
    mcp_changes = []   # [(服务名, 新增/已更新)]
    for name, conf in servers.items():
        old = mcp.get(name)
        if old is None:
            mcp[name] = conf
            mcp_changes.append((name, "新增"))
        elif old != conf:
            mcp[name] = conf
            mcp_changes.append((name, "已更新"))
    if mcp_changes:
        _save_json(mcp_path, data)

    claude_dir = os.path.join(project_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    settings_path = os.path.join(claude_dir, "settings.local.json")
    data, err = _load_json_obj(settings_path)
    if err:
        return None, err
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        return None, "settings.local.json 的 permissions 不是JSON对象"
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        return None, "settings.local.json 的 permissions.allow 不是JSON数组"
    add_allow = [x for x in allow_rules if x not in allow]
    en = data.setdefault("enabledMcpjsonServers", [])
    if not isinstance(en, list):
        return None, "settings.local.json 的 enabledMcpjsonServers 不是JSON数组"
    add_enabled = [n for n in servers if n not in en]
    # 同名key覆盖：enableAllProjectMcpServers 存在但非 true 时改为 true
    had_all_mcp = "enableAllProjectMcpServers" in data
    all_mcp_ok = data.get("enableAllProjectMcpServers") is True
    st_changes = []
    if add_allow:
        allow.extend(add_allow)
        st_changes.append("新增权限规则 %d 条" % len(add_allow))
    if add_enabled:
        en.extend(add_enabled)
        st_changes.append("新增启用MCP服务：" + "、".join(add_enabled))
    if not all_mcp_ok:
        data["enableAllProjectMcpServers"] = True
        st_changes.append("新增 enableAllProjectMcpServers" if not had_all_mcp
                          else "enableAllProjectMcpServers 已改为 true")
    if st_changes:
        _save_json(settings_path, data)

    msg = ("已完成配置（目录 %s）：\n"
           ".mcp.json：%s\n"
           ".claude\\settings.local.json：%s" % (
               project_dir,
               "；".join("%s %s" % (n, act) for n, act in mcp_changes)
               or "已一致，未修改",
               "；".join(st_changes) or "已一致，未修改"))
    return msg, None


def create_app(tray_on_change=None, tray_notify=None):
    ensure_dirs()
    config = Config()
    store = CookieStore()
    capture = CaptureManager(config)
    # 抓包开始/结束的托盘气泡通知回调（main.py 注入经托盘 icon.notify）：
    # MCP 工具与 Web 接口共用同一 capture 实例，两种触发方式均生效
    capture.on_tray_notify = tray_notify
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

    # ---------------- MCP 服务（SSE 协议，URI 区分：/mcp/sse） ----------------
    # 工具与 REST 端点同源（同一批 capture/store/executor/config 实例）。
    # mcp 库缺失在 main.py 启动序前置检查（发现即退出并弹窗提示执行
    # install.bat），此处不再需要降级分支。
    mcp = mcpserver.create_mcp_server(config, capture, store, executor)
    # sse_app 不传 mount_path：mcp 库 connect_sse 会自动拼 scope["root_path"]
    # （挂载前缀），再传会把前缀拼两遍（/mcp/mcp/messages/）
    app.mount(mcpserver.MCP_MOUNT_PATH, mcp.sse_app())

    @app.get("/api/mcp/tools")
    async def mcp_tools():
        """MCP 工具清单（名称/中文描述/参数 schema），
        前端"MCP服务说明"TAB 展示用。"""
        tools = await mcp.list_tools()
        return {"tools": [
            {"name": t.name, "description": t.description or "",
             "inputSchema": t.inputSchema} for t in tools]}

    # ---------------- 页面 ----------------
    @app.get("/", response_class=HTMLResponse)
    async def index():
        with open(os.path.join(STATIC_DIR, "index.html"), "r",
                  encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # ---------------- 状态 ----------------
    @app.get("/api/extension-dir")
    async def extension_dir():
        """Chrome 插件目录路径（页头"安装Chrome插件-人工"按钮复制给用户加载）。"""
        return {"path": EXTENSION_DIR}

    # ---------------- Chrome 插件自动安装（需求「安装Chrome插件-自动」） ----------------
    # 更新源（CRX + update.xml）由本服务提供：注册表策略安装后程序自动操作
    # Chrome 在 chrome://policy 点"重新加载政策"，Chrome 随即经
    # http://127.0.0.1:{port}/crx/ 拉取安装（Chrome 未运行时下次启动生效）。

    @app.get("/crx/update.xml")
    async def crx_update_xml():
        from . import ext_install
        p = os.path.join(ext_install.WORK_DIR, "update.xml")
        if not os.path.isfile(p):
            return JSONResponse({"error": "尚未生成（先执行注册表安装）"},
                                status_code=404)
        # 记录被拉取时间：安装流程以此确认 Chrome 已发起更新检查
        ext_install.note_crx_fetch("update_xml")
        return FileResponse(p, media_type="text/xml")

    @app.get("/crx/chrome_capture_operate.crx")
    async def crx_file():
        from . import ext_install
        p = os.path.join(ext_install.WORK_DIR, "chrome_capture_operate.crx")
        if not os.path.isfile(p):
            return JSONResponse({"error": "尚未生成（先执行注册表安装）"},
                                status_code=404)
        ext_install.note_crx_fetch("crx")
        return FileResponse(p,
                            media_type="application/x-chrome-extension")

    @app.post("/api/extension/registry_install")
    async def extension_registry_install():
        """注册表策略安装：打包 CRX → 写更新源 → 写 Forcelist 策略 →
        自动操作 Chrome 在 chrome://policy 点"重新加载政策"（约 10 秒，
        期间请勿操作键鼠），使扩展立即安装。

        策略写入需管理员：锁死时自动写 reg 文件并调 regedit.exe 导入
        （UAC 弹出仅限该步骤，需人工点击确认）。后台任务，前端轮询状态。
        """
        from . import ext_install

        def fn(log_fn):
            return ext_install.registry_install_run(
                EXTENSION_DIR, config.get("port"), log_fn=log_fn)
        if not ext_install.task.start("registry_install", fn, timeout=180):
            return JSONResponse(
                {"ok": False, "error": "已有安装/卸载任务在进行"},
                status_code=400)
        return {"ok": True, "started": True}

    @app.post("/api/extension/registry_uninstall")
    async def extension_registry_uninstall():
        """注册表一键卸载：删策略（UAC）→ 自动在 chrome://policy 点
        "重新加载政策"使删除立即生效（失败退回盲等周期刷新）→ UI 自动移除。

        后台任务（全程约 1~4 分钟，前端轮询状态）。
        """
        from . import ext_install
        if not ext_install.start_uninstall_task(
                ext_install.registry_ext_id()):
            return JSONResponse(
                {"ok": False, "error": "已有安装/卸载任务在进行"},
                status_code=400)
        return {"ok": True, "started": True}


    @app.post("/api/extension/ui_install")
    async def extension_ui_install():
        """模拟点击安装（UI 自动化，约 10 秒，期间人工勿动键鼠）。"""
        from . import ext_install
        if not ext_install.start_ui_install_task(EXTENSION_DIR):
            return JSONResponse(
                {"ok": False, "error": "已有安装/卸载任务在进行"},
                status_code=400)
        return {"ok": True, "started": True}

    @app.get("/api/extension/install_status")
    async def extension_install_status():
        """安装/卸载后台任务状态（前端轮询：运行中/结果/日志尾部）。"""
        from . import ext_install
        st = ext_install.task.status()
        st["deps"] = await asyncio.to_thread(ext_install.deps_check)
        return st

    # ---------------- Chrome插件设置页（"安装Chrome插件-自动"页设置按钮） ----------------
    # 方案见 docs/chrome插件网页修改配置/README.md（externally_connectable
    # 消息通道 get_config/set_config，v1.1.4 实测）：页面经本服务（127.0.0.1）
    # 访问即与插件通道同源，程序调用读取/修改并回读验证。

    @app.get("/extension-settings", response_class=HTMLResponse)
    async def extension_settings_page():
        """Chrome插件设置页（新标签页打开）：程序调用读取、修改并回读
        验证插件配置（推送范围/清单/节流间隔等）。"""
        with open(os.path.join(STATIC_DIR, "ext_settings.html"), "r",
                  encoding="utf-8") as f:
            return HTMLResponse(f.read())

    @app.get("/api/extension/id_candidates")
    async def extension_id_candidates():
        """插件设置页连接插件用的候选扩展 ID（按序尝试）：
        人工配置的解压版 ID（全局配置 extension_id）+ 注册表安装版 ID
        （策略值/update.xml 推出）。"""
        from . import ext_install
        ids = []
        conf = (globalconf.get_value(globalconf.KEY_EXTENSION_ID)
                or "").strip()
        if conf:
            ids.append(conf)
        reg = ext_install.registry_ext_id()
        if reg and reg not in ids:
            ids.append(reg)
        return {"ids": ids}

    # ---------------- 插件 cookie 推送范围读写接口（页面与 AI 共用） ----------------
    # 需求：「Chrome插件设置」页与 AI 使用同一个 HTTP 接口完成插件推送
    # 范围的读取与修改，以起到验证作用。实现：接口经 extcmd 机制触达
    # 无 CDP 日常 Chrome 中的插件——chrome.exe 打开命令页（/extension-cmd）
    # → 命令页经 externally_connectable 消息通道执行 get_config/set_config
    # → 结果回传 /api/extension/cmd_report（docs 已实测方案）。

    @app.get("/extension-cmd", response_class=HTMLResponse)
    async def extension_cmd_page():
        """插件指令命令页（服务内部经 chrome.exe 在日常 Chrome 打开的
        自动化通道页：自动执行指令并把结果回传，随后自关尝试）。"""
        with open(os.path.join(STATIC_DIR, "extension_cmd.html"), "r",
                  encoding="utf-8") as f:
            return HTMLResponse(f.read())

    @app.post("/api/extension/cmd_report")
    async def extension_cmd_report(request: Request,
                                   token: str = Query(...)):
        """命令页结果回传（一次性 token 匹配等待中的调用；重复/无效
        token 返回 ok=False 不影响原调用等待超时报错）。"""
        from . import extcmd
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "回传内容非 JSON"},
                                status_code=400)
        return {"ok": extcmd.report(token, body)}

    @app.get("/api/extension/config")
    async def extension_config_get():
        """读取插件当前 cookie 推送范围配置（「Chrome插件设置」页与 AI
        共用的同一接口，以起到验证作用）。

        经命令页在日常 Chrome 中执行 get_config 后返回（含命中的扩展
        ID）；插件未装/无候选 ID 时返回错误。阻塞约 2~5 秒（超时 20 秒）。
        """
        from . import extcmd
        r = await asyncio.to_thread(
            extcmd.run_ext_command, config.get("port"), "get_config")
        return r

    class ExtConfigBody(BaseModel):
        push_scope: str = None   # none（全部禁止）/ list（按清单）/ all（全部允许）
        allow_list: list = None  # push_scope=list 的允许清单（字符串数组）
        deny_list: list = None   # 不允许清单（不允许优先）

    @app.post("/api/extension/config")
    async def extension_config_set(body: ExtConfigBody):
        """修改插件 cookie 推送范围设置（「Chrome插件设置」页与 AI 共用
        的同一接口）：经命令页执行 set_config 并自动 get_config 回读
        验证，返回是否回读一致。"""
        if body.push_scope is not None and body.push_scope not in (
                "none", "list", "all"):
            return JSONResponse(
                {"ok": False, "error": "push_scope 只能是 none/list/all"},
                status_code=400)
        params = {}
        if body.push_scope is not None:
            params["push_scope"] = body.push_scope
        if body.allow_list is not None:
            params["allow"] = ",".join(str(x) for x in body.allow_list)
        if body.deny_list is not None:
            params["deny"] = ",".join(str(x) for x in body.deny_list)
        if not params:
            return JSONResponse(
                {"ok": False, "error": "未提供任何配置字段（push_scope/"
                                      "allow_list/deny_list）"},
                status_code=400)
        from . import extcmd
        r = await asyncio.to_thread(
            extcmd.run_ext_command, config.get("port"), "set_config",
            params)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)


    # 使用说明中的截图（项目根 pics/ 目录）：/pics/<文件名> 直接访问，
    # README.md 中以 ![说明](pics/xxx.png) 引用
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

    @app.post("/api/chrome/bring_to_front")
    async def chrome_bring_to_front():
        """把用于抓包的 Chrome 窗口还原（若最小化）并置顶显示。

        抓包页与"AI自主操作Chrome"弹窗的"启动用于抓包的Chrome"按钮在
        Chrome 已启动时变为置顶按钮（需求），点击调本接口。
        """
        ok, msg = await asyncio.to_thread(
            chrome_proc.bring_chrome_to_front, config.get("cdp_port"))
        if not ok:
            return JSONResponse({"ok": False, "message": msg}, status_code=400)
        return {"ok": True, "message": msg}

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
        ops_inject_enabled: bool = True

    @app.put("/api/capture/config")
    async def capture_put_conf(conf: CaptureConf):
        # 会话级配置，不写 conf.json；抓包中调整对后续数据即时生效
        # 抓包中仅与触发方一致的来源可修改配置（避免人工与AI同时操作）
        ok, err = capture.check_source("web")
        if not ok:
            return JSONResponse({"ok": False, "message": err}, status_code=400)
        capture.capture_conf.update(conf.dict())
        # "记录前端页面操作"勾选框：抓包中途开关即时生效（补注入/拆除），
        # 不只对下一次开始抓包生效
        if capture.state in ("capturing", "paused"):
            await capture.set_ops_inject(conf.ops_inject_enabled,
                                         source="web")
        return {"ok": True}

    @app.get("/api/capture/hosts")
    async def capture_hosts():
        return {"hosts": sorted(capture.seen_hosts)}

    @app.post("/api/capture/hosts/clear")
    async def capture_hosts_clear():
        """清空累计访问过的网址清单（抓包配置页"清除记录"按钮）。"""
        n = capture.clear_hosts()
        await ws_clients.broadcast({"type": "hosts", "hosts": []})
        return {"ok": True, "cleared": n}

    class StartBody(BaseModel):
        inject_ops: bool = None

    @app.post("/api/capture/start")
    async def capture_start(body: StartBody = None):
        """开始抓包。可选 JSON body {"inject_ops": true} 控制是否注入
        DOM 操作监听（缺省读 capture_conf 的 ops_inject_enabled 键）。"""
        inject = body.inject_ops if body else None
        ok, msg = await capture.start(source="web", inject_ops=inject)
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/pause")
    async def capture_pause():
        ok, msg = await capture.pause(source="web")
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/resume")
    async def capture_resume():
        ok, msg = await capture.resume(source="web")
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/stop")
    async def capture_stop():
        ok, msg = await capture.stop(source="web")
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/purge")
    async def capture_purge():
        ok, msg = await capture.purge()
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    @app.post("/api/capture/mark")
    async def capture_mark():
        ok, msg = await capture.mark(source="web")
        return JSONResponse({"ok": ok, "message": msg},
                            status_code=200 if ok else 400)

    class NoteBody(BaseModel):
        text: str
        source: str = "web"

    @app.post("/api/capture/note")
    async def capture_note(body: NoteBody):
        """抓包期间记录操作注释（时间点留痕，写入 index.md 统一时间线）。

        优化建议 2026-09-27 建议 1：AI/人工在前端页面执行操作前后留痕，
        便于分析时精确对齐前端操作与抓到的请求；与 mark（会话目录改名）
        语义不同。
        """
        ok, msg = await capture.note(body.text, source=body.source)
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
        return session_path(name)

    @app.get("/api/history")
    async def history():
        # 只统计记录数（按文件名匹配，不做 getsize——stat 在 Windows 下很慢）；
        # 放后台线程避免阻塞事件循环
        def _collect():
            return [{k: i[k] for k in ("name", "record_count", "path")}
                    for i in list_history_sessions()]

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
        return parse_index_records(index_path)

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
            return purge_history_session(p, session, body.dict(), config)

        deleted, kept = await asyncio.to_thread(_do)
        return {"ok": True, "deleted": deleted, "kept": kept,
                "message": "已删除 %d 条被过滤的记录（如静态资源），保留 %d 条" % (
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
            p, os.path.join(os.path.dirname(p), new_name))
        if ok:
            # 重命名的是最近会话时同步跟踪（rename_capture_session 的
            # 空 session 解析与状态展示保持一致）
            if session == capture.session_name:
                capture.session_name = new_name
                capture.session_dir = os.path.join(os.path.dirname(p),
                                                   new_name)
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
        ok, err = rename_with_retry(
            p, os.path.join(os.path.dirname(p), new_name))
        if not ok:
            return JSONResponse({"error": "重命名失败: %s" % err},
                                status_code=500)
        # 重命名的是最近会话时同步跟踪（同标记接口）
        if session == capture.session_name:
            capture.session_name = new_name
            capture.session_dir = os.path.join(os.path.dirname(p), new_name)
        return {"ok": True, "name": new_name, "changed": True}

    @app.post("/api/history/{session}/prompt")
    async def history_prompt(session: str):
        p = _session_path(session)
        if not p:
            return JSONResponse({"error": "目录不存在"}, status_code=404)
        # 生成前必须已配置生成的脚本保存根目录（全局配置
        # python_scripts_dir_path，在快速执行脚本页面配置）
        scripts_root = (globalconf.get_value(
            globalconf.KEY_SCRIPTS_DIR) or "").strip()
        if not scripts_root:
            return JSONResponse(
                {"error": "尚未配置保存生成的Python脚本文件根目录，"
                          "请先在快速执行脚本页面配置"},
                status_code=400)
        # 结构化提示词模板（对应 prompt 需求“生成提示词”节）；
        # 两个“需要人工填写”节由人工在弹窗中编辑后复制给 AI；
        # 路径前后带空格，便于 AI 识别路径边界
        text = (
            "# 提供数据\n"
            "%s 为浏览器访问抓包记录目录，需要根据文件内容进行分析，"
            "首先读取index.md汇总文件，再按序号查看明细文件\n"
            "%s 需要根据文件内容了解Python脚本生成的执行环境与依赖约束等\n"
            "# 分析前端页面\n"
            "假如仅通过抓包数据无法分析网站请求逻辑，AI可使用chrome-devtools-attach MCP工具操作浏览器分析前端页面，可参考 %s 建议\n"
            "操作页面期间假如正在抓包，每执行一个前端操作（点击/填写/选择）前后"
            "调用 POST /api/capture/note 记录注释"
            "（body为json，参数text，内容如\"AI操作: <做了什么，预期触发什么>\"），"
            "便于分析时精确对齐前端操作与抓到的请求\n"
            "假如开始抓包时开启了\"记录前端页面操作\"（DOM事件监听注入），"
            "页面上的人工操作会自动记录到 index.md 统一时间线中，"
            "无需人工调用 note\n"
            "# 验证要求\n"
            "写脚本前，对从抓包/页面推断出的每个关键假设（filter参数格式、可选值、"
            "分页行为、空值与日期等边界语义）先用 /api/cookies/cdp 获取登录态、"
            "直连接口实测验证，验证结果（含实测数字）写入 baseline.md\n"
            "注意：页面UI生成的请求格式未必是接口的有效格式，以直连实测为准；"
            "\"返回0条\"既可能是格式无效也可能是数据确实为空，"
            "需换一组已知有数据的条件交叉验证后再下结论\n"
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
            "## baseline.md\n"
            "按 %s 的基线保存约定生成数据来源契约表，"
            "记录每个数据来源的参数格式、可选值、实测结果等\n"
            "## prompt.md\n"
            "记录本次使用的完整提示词（含人工补充的操作描述与具体要求）\n"
            "# 人工在网页的操作描述\n"
            "（需要人工填写：说明当前进行了什么操作，有哪些页面有展示的重要的值是什么）\n"
            "# 生成Python脚本具体要求\n"
            "（需要人工填写：说明生成的Python脚本需要执行什么功能，是否有入参，执行逻辑是什么）\n"
        ) % (os.path.abspath(p), os.path.abspath(API_DOC_PATH),
             os.path.abspath(ANALYSIS_GUIDE_PATH),
             os.path.abspath(scripts_root),
             os.path.abspath(ANALYSIS_GUIDE_PATH))
        return {"prompt": text}

    # ---------------- Cookie ----------------
    class PushBody(BaseModel):
        reason: str = "未知"
        cookies: list = []
        profile: int = 0   # Chrome profile（多开实例）编号，未设置为 0
        authorizations: list = []   # 插件观察到的 Authorization（{host, value}；旧扩展不传）

    @app.post("/api/cookies/push")
    async def cookies_push(body: PushBody, request: Request):
        source = request.client.host if request.client else ""
        ok, count, auth_count = store.receive(
            body.cookies, body.reason, source, body.profile,
            body.authorizations)
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
            # 数量为 0 时广播红色提醒（prompt 需求 接收Cookie功能要求）：
            # 可能是插件未设置推送Cookie范围（默认全部禁止）
            if count == 0 and auth_count == 0:
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
        """profile 为 0（默认）时查所有 Chrome profile；非 0 查指定编号。

        Authorization 按 Cookie 请求的同一主机精确匹配（不跨域携带）；
        cookie 与 Authorization 均无匹配才返回 404（向下兼容：cookie 命中
        或 Authorization 命中即 200；纯 Authorization 站点 cookies 为空数组）。
        """
        cookies, err = store.query(url, profile)
        auth = store.query_auth(url, profile)
        if err and auth is None:
            log.info("获取cookie请求 url=%s profile=%d 失败: %s",
                     url, profile, err)
            cookie_query_log.log_cookie_query(
                "web", url, "", ok=False, scope="profile=%d" % profile,
                error=err)
            return JSONResponse({"ok": False, "error": err}, status_code=404)
        # 纯 Authorization 站点：query 返回 None，归一为空数组（响应字段
        # 类型稳定，旧脚本按列表处理不炸）
        if cookies is None:
            cookies = []
        # 只记录数量与 key，不记录 cookie 值（日志脱敏要求）
        keys = ",".join(c.get("name", "") for c in cookies)
        log.info("获取cookie请求 url=%s profile=%d 返回 %d 条 cookie（key: %s）",
                 url, profile, len(cookies), keys or "无")
        h = url.strip()
        if "://" in h:
            h = urlsplit(h).hostname or ""
        else:
            h = h.split("/")[0].split(":")[0]
        cookie_query_log.log_cookie_query(
            "web", url, h.lower(), ok=True, count=len(cookies),
            keys=[c.get("name", "") for c in cookies],
            auth_hit=bool(auth), scope="profile=%d" % profile)
        authorizations = ([{"host": h.lower(), "value": auth}] if auth else [])
        return {"ok": True,
                "cookies": cookies,
                "cookie_header": CookieStore.cookie_header(cookies),
                "authorizations": authorizations,
                "authorization": auth or ""}

    @app.get("/api/cookies/cdp")
    async def cookies_cdp(url: str = Query(...), port: int = Query(0)):
        """经 CDP 实时读取抓包 Chrome 的 Cookie 与观察到的 Authorization。

        Cookie 实时来自 Storage.getCookies（含 httpOnly/分区 Cookie）；
        Authorization 来自抓包期间观察到的请求头缓存（精确主机匹配）。
        与 /api/cookies/query 互补（query=日常 Chrome 插件快照）。
        port=0 时用配置的 cdp_port；port 仅影响 Cookie 读取，Authorization
        观察来自配置实例的抓包事件流。
        """
        cdp_port = port or config.get("cdp_port")
        host = cookie_cdp.parse_host(url)
        if not host:
            return JSONResponse(
                {"ok": False, "error": "缺少 url 参数或无法解析主机名"},
                status_code=404)
        cookies, cookie_err = await cookie_cdp.query_cdp(url, cdp_port)
        auth_entry = cookie_cdp.auth_cache.get(host)
        # 与现有 query 兼容口径一致：cookie 与 Authorization 均无命中才 404
        if cookie_err and not auth_entry:
            log.info("CDP获取cookie请求 url=%s port=%d 失败: %s",
                     url, cdp_port, cookie_err)
            cookie_query_log.log_cookie_query(
                "cdp", url, host, ok=False, scope="port=%d" % cdp_port,
                error=cookie_err)
            return JSONResponse({"ok": False, "error": cookie_err},
                                status_code=404)
        if cookies is None:
            cookies = []      # 纯 Authorization 命中：归一为空数组
        keys = ",".join(c.get("name", "") for c in cookies)   # 日志脱敏：只记 key
        log.info("CDP获取cookie请求 url=%s port=%d 返回 %d 条 cookie（key: %s）"
                 " auth=%s", url, cdp_port, len(cookies), keys or "无",
                 "有" if auth_entry else "无")
        cookie_query_log.log_cookie_query(
            "cdp", url, host, ok=True, count=len(cookies),
            keys=[c.get("name", "") for c in cookies],
            auth_hit=bool(auth_entry), scope="port=%d" % cdp_port)
        authorizations = ([{"host": host, "value": auth_entry["value"],
                            "observed_at": auth_entry["observed_at"]}]
                          if auth_entry else [])
        resp = {"ok": True, "source": "cdp", "cdp_port": cdp_port,
                "cookies": cookies,
                "cookie_header": CookieStore.cookie_header(cookies),
                "authorizations": authorizations,
                "authorization": auth_entry["value"] if auth_entry else "",
                "cookie_error": cookie_err or ""}
        return resp

    @app.get("/api/cookies/receives")
    async def cookies_receives():
        return {"receives": store.receives(), "total": store.total(),
                "last_push_time": store.last_push_time,
                "log_dir": os.path.abspath(LOG_DIR)}

    @app.get("/api/cookies/queries")
    async def cookies_queries():
        """当天最新 50 条 Cookie 查询记录（安全留痕）。

        数据从当天结构化日志文件读取（cookie_query_log 模块，服务重启
        不丢）；完整审计在 log/cookie_query.log（每行一条 JSON，每天
        滚动、不自动删除）。log_dir 供页面展示日志查看说明。
        """
        return {"queries": cookie_query_log.queries(),
                "log_dir": os.path.abspath(LOG_DIR)}

    # ---------------- 使用说明 ----------------
    @app.get("/api/usage")
    async def usage():
        """使用说明内容（项目根目录 README.md，前端按 markdown 渲染）。

        README 同时作为推广文档与 Web 页"使用说明"标签页的单一来源。"""
        path = os.path.join(PROJECT_ROOT, "README.md")
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
        # 生成的脚本保存根目录：必须是存在的合法目录路径
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

    # ---------------- 系统托盘快速执行脚本菜单（全局配置 tray_scripts） ----------------
    # 托盘菜单由 main.start_tray 读取该列表构建；增删后回调 tray_on_change
    # 让托盘线程重建菜单（无需重启程序）。脚本校验复用 executor 的
    # validate_script_path（必须位于脚本根目录内且为 .py）。

    def _tray_scripts():
        return globalconf.get_json(globalconf.KEY_TRAY_SCRIPTS, [])

    def _tray_valid(p):
        from .executor import validate_script_path
        return bool(validate_script_path(p))

    @app.get("/api/tray_scripts")
    async def tray_scripts_list():
        return {"scripts": _tray_scripts()}

    @app.post("/api/tray_scripts/add")
    async def tray_scripts_add(body: RunBody):
        p = body.path
        if not _tray_valid(p):
            return JSONResponse(
                {"error": "脚本不存在或不在允许的脚本目录内"},
                status_code=400)
        items = _tray_scripts()
        if p not in items:
            items.append(p)
            globalconf.set_json(globalconf.KEY_TRAY_SCRIPTS, items)
            log.info("已添加到系统托盘快速执行菜单: %s", p)
        if tray_on_change:
            tray_on_change()
        return {"ok": True, "scripts": items}

    @app.post("/api/tray_scripts/remove")
    async def tray_scripts_remove(body: RunBody):
        items = _tray_scripts()
        if body.path in items:
            items.remove(body.path)
            globalconf.set_json(globalconf.KEY_TRAY_SCRIPTS, items)
            log.info("已从系统托盘快速执行菜单移除: %s", body.path)
        if tray_on_change:
            tray_on_change()
        return {"ok": True, "scripts": items}

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
        op_event_types: list = None

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

    @app.get("/api/ai_prompt")
    async def ai_prompt():
        """AI 自主操作提示词模板（页头"AI自主操作Chrome"按钮弹窗）。

        与"生成提示词"（人工操作后供 AI 分析抓包数据）不同：此模板面向
        AI Agent 自主操作——经 chrome-devtools-attach 操控页面、
        chrome-operate 开关抓包，完成后生成脚本。{api.md 路径}按
        需求动态获得；打开弹窗时前端自动复制到剪贴板（另有复制按钮）。
        """
        scripts_root = (globalconf.get_value(
            globalconf.KEY_SCRIPTS_DIR) or "").strip()
        lines = [
            "# MCP工具",
            "使用 chrome-devtools-attach 操作当前打开的 Chrome 页面；"
            "chrome-operate 在需要时开启抓包，完毕后结束",
            "抓包目录中有索引文件index.md，每次请求的数据保存在对应序号的.md文件",
            "可参考 %s 文件（AI分析前端页面与抓包内容的建议方法）" % (
                os.path.abspath(ANALYSIS_GUIDE_PATH)),
            "抓包进行期间，每在前端页面执行一个操作（点击/填写/选择）前后"
            "调用 chrome-operate 的 note_capture 工具记录注释"
            "（做了什么、预期触发什么请求），便于精确对齐前端操作与抓到的请求",
            "开始抓包时开启 inject_ops（DOM操作监听注入）可自动记录前端页面操作"
            "（含人工操作的输入值与提交事件），无需逐个调用 note_capture",
            "# 任务",
            "先在页面上用不同筛选条件各查几次并抓包，分析前端操作与"
            "调用后台接口的关系（注意数据未必都走接口：可能内嵌在页面"
            "里，或纯前端过滤）。抓包记录保存在会话目录里，通过 "
            "chrome-operate 获取路径",
            "# 验证要求",
            "写脚本前，对从抓包/页面推断出的每个关键假设（filter参数格式、"
            "可选值、分页行为、空值与日期等边界语义）先用 /api/cookies/cdp "
            "获取登录态、直连接口实测验证，验证结果（含实测数字）写入 "
            "baseline.md",
            "注意：页面UI生成的请求格式未必是接口的有效格式，以直连实测为准；"
            "\"返回0条\"既可能是格式无效也可能是数据确实为空，"
            "需换一组已知有数据的条件交叉验证后再下结论",
            "需要在 %s 下生成一个子目录，子目录名根据内容取合适的中英文"
            "名称，在该子目录中生成以下文件：" % (
                scripts_root or
                "{全局配置文件，key=python_scripts_dir_path的值}"),
            "## Python脚本",
            "运行环境、依赖、Cookie 获取等写法看 %s。请求中使用的数据"
            "需要来自用户指定的值，或通过网站提供的查询接口获取，尽量"
            "不要硬编码；假如存在参数值无法确定来源，需要提醒人工确认，"
            "可能是抓包内容缺少了获取对应数据的请求" % (
                os.path.abspath(API_DOC_PATH)),
            "## README.md",
            "说明当前脚本的作用、使用说明等",
            "## baseline.md",
            "按以上 analysis-guide 建议方法的基线保存约定生成数据来源契约表，"
            "记录每个数据来源的参数格式、可选值、实测结果等",
            "## prompt.md",
            "记录本次使用的完整提示词（含人工补充的操作描述与具体要求）",
            "# 要求填写",
            "## 需要AI在前端页面执行哪些操作",
            "（需要人工填写：说明需要打开哪些菜单或页面，需要使用哪些"
            "条件触发哪些操作，需求、技术文档有哪些）",
            "## 需要生成的Python脚本的具体要求",
            "（需要人工填写：说明生成的Python脚本需要执行什么功能，"
            "是否有入参，执行逻辑是什么）",
        ]
        return {"prompt": "\n".join(lines),
                "scripts_root_configured": bool(scripts_root)}

    @app.get("/api/api_md_path")
    async def api_md_path():
        return {"path": os.path.abspath(API_DOC_PATH),
                "project_root": PROJECT_ROOT}

    # ---------------- AI自主操作Chrome：Claude Code 项目 MCP 一键配置 ----------------
    class ClaudeCodeConfBody(BaseModel):
        path: str = ""

    @app.post("/api/aiops/claude_code_config")
    async def aiops_claude_code_config(body: ClaudeCodeConfBody):
        r"""在指定的 Claude Code 项目目录写入 MCP 配置（页头
        "AI自主操作Chrome"→b. AI Agent MCP配置→使用Claude Code 的
        "配置"按钮）。

        写入 .mcp.json（mcpServers）与 .claude\settings.local.json（权限
        与 MCP 启用），文件不存在则创建，已存在则增加缺失配置、同名key
        的value覆盖更新，无关配置不动。端口按当前参数配置
        （cdp_port/port）填充。
        """
        d = (body.path or "").strip().strip('"').strip()
        if not d:
            return JSONResponse(
                {"ok": False, "error": "请输入需要作为Claude Code项目目录的完整路径"},
                status_code=400)
        if not os.path.isabs(d):
            return JSONResponse(
                {"ok": False, "error": "需要输入完整路径：%s" % d},
                status_code=400)
        if not os.path.isdir(d):
            return JSONResponse(
                {"ok": False, "error": "目录不存在：%s" % d},
                status_code=400)
        d = os.path.abspath(d)
        msg, err = await asyncio.to_thread(
            merge_claude_code_conf, d,
            config.get("cdp_port"), config.get("port"))
        if err:
            log.warning("AI自主操作Chrome-Claude Code配置失败: 目录=%s 错误=%s",
                        d, err)
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        log.info("AI自主操作Chrome-Claude Code配置: 目录=%s 结果=%s",
                 d, msg.replace("\n", "；"))
        return {"ok": True, "message": msg}

    return app
