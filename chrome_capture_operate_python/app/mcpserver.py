"""MCP 服务：FastMCP（SSE 协议）挂载到 Web 应用的 /mcp 路径。

prompt 需求「MCP功能」：
- HTTP 端口同时提供 SSE 协议（URI 区分，挂载点 /mcp，SSE 端点 /mcp/sse）；
- 工具与 Web REST 端点同源（同一批 capture/store/executor/config 实例，
  语义一致），AI Agent 经 MCP 可完成"启动抓包 Chrome→抓包→执行生成的脚本"
  全流程闭环；
- 执行生成的脚本工具为同步完成语义：等待结束或超时后终止进程（区别于
  Web 的超时保留人工决策），输出按最大行数与最大字节数取交集截尾；
- mcp 库 pin >=1.8,<2：2.x 将 FastMCP 改名 MCPServer（破坏性变更）。

需求工具清单缺「恢复抓包」（Web 有暂停/恢复，Agent 亦需要），已补上；
「查询接收到的Chrome Cookie」需求未给输入/输出正文，按核心用途实现：
输入 url + 可选 profile，输出该主机 Cookie（浏览器语义匹配）与
Authorization（与 /api/cookies/query 同口径）。
"""
import asyncio
import logging
import os

from pydantic import Field

from . import chrome_proc
from . import config as config_mod
from .capture import (list_history_sessions, purge_history_session,
                      session_path)

log = logging.getLogger("app.mcp")

MCP_MOUNT_PATH = "/mcp"


def clip_output(text, max_lines, max_bytes):
    """输出截断：同时满足行数与字节约束，超出保留最后部分（尾部）。

    先按行截尾再按字节截尾——"取最大行数与最大字节数都满足的最小值"；
    字节截断按 UTF-8 编码取尾部字节，半个多字节字符用 replace 容错。
    """
    if not text:
        return text
    if max_lines and max_lines > 0:
        lines = text.splitlines()
        if len(lines) > max_lines:
            text = "\n".join(lines[-max_lines:])
    if max_bytes and max_bytes > 0:
        data = text.encode("utf-8")
        if len(data) > max_bytes:
            text = data[-max_bytes:].decode("utf-8", "replace")
    return text


def _server_instructions():
    """MCP 使用说明（协议 server instructions 字段，客户端 initialize
    时下发；prompt 需求「### 说明：需要说明当前提供的MCP工具怎样使用」。"""
    return (
        "chrome_capture_operate MCP 服务使用说明\n"
        "\n"
        "本服务提供 HTTP 请求抓包与生成的脚本执行能力。典型工作流：\n"
        "1. start_capture_chrome 启动用于抓包的 Chrome（独立 profile、CDP 调试端口 9222），人工在该 Chrome 中操作目标网站；\n"
        "2. start_capture 开始抓包（inject_ops=true 时注入 DOM 操作监听，前端页面操作自动留痕与请求对齐），人工操作产生的网络访问被保存到 captured_record/<时间目录>/；数据读取：index.md 为汇总——抓包中实时写入（行序为到达顺序，分析时按'请求时间'列排序），结束抓包后已按时间全量重排（请求行与前端操作行统一时间线）；明细文件按序号命名：请求 {0000000001}.md、前端操作 {fr00000001}.md。期间可 get_capture_hosts 查看访问过的网址、set_capture_filters 调整过滤条件（会话级即时生效）、pause_capture/resume_capture 暂停恢复、purge_filtered_records 删除被过滤的记录（session 为空对当前抓包会话执行，非空对已结束会话的指定子目录执行）、note_capture 补充记录操作注释（开启 inject_ops 后前端操作已自动留痕，无需逐个调用）、rename_capture_session 在抓包结束后给会话目录增加或修改描述后缀并可附加人工标记（时间戳前缀保留，目录便于识别本会话做了什么）。注意：开始抓包会记录触发方式（人工网页/MCP工具），暂停/继续/结束抓包、修改过滤条件、人工标记等操作需与触发方式一致（人工网页开始的抓包MCP工具不能操作，反之亦然），避免人工与AI同时操作；\n"
        "3. stop_capture 结束抓包（Chrome 继续保留）；结束后可用 list_capture_sessions 查询抓包目录下的子目录（含记录数与完整路径，人工标记的目录名以 _人工标记 结尾），获取本次或历史会话目录；\n"
        "4. 人工依据抓包数据生成 Python 脚本（保存在脚本目录下；api 目录的文档（api.md、analysis-guide.md）会在人工复制的提示词中指定，此说明中不再赘述）；\n"
        "5. list_scripts 查看生成的脚本，run_script 执行（同步等待，timeout 可覆盖配置超时，输出按 max_lines/max_bytes 截尾保留）；脚本内经 HTTP 接口获取登录态（query_cookies 同口径：浏览器语义 Cookie + 精确主机 Authorization）；\n"
        "6. query_cookies 随时查询当前接收到的 Chrome Cookie。\n"
        "7. chrome插件自动安装：registry_install_extension 注册表策略安装（打包CRX到服务目录crx，UAC需人工确认；安装后自动操作Chrome在chrome://policy点重新加载政策使插件立即安装，并打开chrome://extensions展示结果）；registry_uninstall_extension 一键卸载（删策略后自动重新加载政策，约1-4分钟后台任务）；ui_install_extension 模拟点击安装（约10秒，期间人工勿动键鼠）；进度经 get_extension_install_status 查询；执行期间桌面周边显示绿色边框（完毕结束），完成或失败时经桌面右下角弹窗通知用户（30秒后自动消失）。\n"
        "\n"
        "生成的脚本获取 Cookie 的接口契约见项目根目录的 api/api.md。\n"
        "服务监听 127.0.0.1（本机），Web 页面地址与 MCP SSE 同端口。"
    )


def create_mcp_server(config, capture, store, executor):
    """构造 FastMCP 实例（工具闭包持有各组件引用），由 webapp 挂载。"""
    from mcp.server import FastMCP

    mcp = FastMCP(name="chrome_capture_operate",
                  instructions=_server_instructions(),
                  sse_path="/sse", message_path="/messages/")

    # ---------- Chrome 抓包进程 ----------

    @mcp.tool(name="start_capture_chrome",
              description="启动用于抓包的Chrome（独立profile、CDP调试端口）。"
                          "已启动则不重复启动。返回是否成功与说明")
    async def start_capture_chrome() -> dict:
        ok, msg = await asyncio.to_thread(
            chrome_proc.start_capture_chrome, config.get("cdp_port"))
        if ok:
            try:
                await capture.ensure_cdp()
            except Exception as e:
                log.warning("MCP启动Chrome后CDP连接失败: %s", e)
        return {"ok": ok, "message": msg}

    @mcp.tool(name="check_capture_chrome",
              description="检测用于抓包的Chrome进程是否已启动（CDP调试端口"
                          "探测）。返回 alive 布尔值")
    async def check_capture_chrome() -> dict:
        alive = await asyncio.to_thread(
            chrome_proc.is_cdp_alive, config.get("cdp_port"))
        return {"alive": bool(alive)}

    # ---------- 抓包控制 ----------

    @mcp.tool(name="start_capture",
              description="开始抓包：将调试端口Chrome的网络访问数据保存到"
                          "文件。返回是否成功与抓包目录说明。开始后本次会话的"
                          "触发方式记录为MCP（AI Agent）。inject_ops 为 true 时"
                          "注入 DOM 操作监听（记录前端页面操作与请求对齐），"
                          "为空时读抓包配置默认")
    async def start_capture(
        inject_ops: bool = Field(
            None, description="是否注入 DOM 操作监听（记录前端操作）："
                              "true=注入，false=不注入，空=读配置默认"),
    ) -> dict:
        ok, msg = await capture.start(source="mcp", inject_ops=inject_ops)
        return {"ok": ok, "message": msg}

    @mcp.tool(name="pause_capture",
              description="暂停抓包：不清空已产生的记录，之后可恢复。仅当本次抓包由MCP开始时可执行（人工网页开始的会返回错误，避免人工与AI同时操作）")
    async def pause_capture() -> dict:
        ok, msg = await capture.pause(source="mcp")
        return {"ok": ok, "message": msg}

    @mcp.tool(name="resume_capture",
              description="恢复抓包：继续之前暂停的抓包会话。仅当本次抓包由MCP开始时可执行（人工网页开始的会返回错误，避免人工与AI同时操作）")
    async def resume_capture() -> dict:
        ok, msg = await capture.resume(source="mcp")
        return {"ok": ok, "message": msg}

    @mcp.tool(name="stop_capture",
              description="结束抓包：落盘收尾并结束当前会话（index.md 按时间"
                          "全量重排，含请求行与前端操作行），返回含会话目录"
                          "与 data_note 数据读取说明。仅当本次抓包由MCP开始"
                          "时可执行（人工网页开始的会返回错误，避免人工与"
                          "AI同时操作）")
    async def stop_capture() -> dict:
        ok, msg = await capture.stop(source="mcp")
        return {
            "ok": ok,
            "message": msg,
            "session_dir": capture.session_dir or "",
            "data_note": ("会话目录中 index.md 已按时间排序（请求行与"
                          "前端操作行统一时间线），从时间线顺序即可确定"
                          "操作与请求的先后；明细文件按序号命名：请求 "
                          "{0000000001}.md、前端操作 {fr00000001}.md；"
                          "可调用 rename_capture_session 为目录增加描述"
                          "后缀（如\"查询工单列表\"）便于识别"
                          if ok else ""),
        }

    @mcp.tool(name="rename_capture_session",
              description="抓包结束后重命名会话目录，便于人工与AI识别本会话"
                          "做了什么：在目录名的时间戳前缀后增加或修改描述"
                          "后缀（无则新增、有则替换不叠加，时间戳前缀始终"
                          "保留），并控制\"_人工标记\"后缀（默认附加）。"
                          "示例：name=\"查询工单列表\"，目录变为 "
                          "2026_09_28_17_05_48_查询工单列表_人工标记。"
                          "仅对已结束的会话可用（正在抓包中不允许）")
    async def rename_capture_session(
        session: str = Field(None, description="会话目录名，可选；空=最近"
                                               "结束的会话（结束抓包返回的"
                                               "目录，最典型用法）"),
        name: str = Field(None, description="目录名的描述后缀，如\"查询工单"
                                            "列表\"：无则新增、有则替换（不"
                                            "叠加）；空=保持现有描述不变。"
                                            "自动过滤Windows非法字符与首尾"
                                            "空格，上限40字符"),
        mark: bool = Field(None, description="是否附加\"_人工标记\"后缀："
                                            "true=确保存在（已带不重复附加），"
                                            "false=移除；默认true"),
    ) -> dict:
        ok, res = capture.rename_session_suffix(
            session, name, True if mark is None else bool(mark))
        if not ok:
            return {"ok": False, "error": res}
        return {"ok": True, "name": res["name"], "path": res["path"],
                "changed": res["changed"]}

    @mcp.tool(name="note_capture",
              description="记录一条操作注释到当前抓包会话时间线（与请求记录、"
                          "前端页面操作行统一按时间排序）：开始抓包开启"
                          "inject_ops后，页面上的前端操作（点击、填写值、提交"
                          "等）已自动留痕，本工具用于补充记录页面留痕覆盖不到"
                          "的信息（如即将执行什么操作、预期触发什么请求），"
                          "无需每个操作都调用。仅当本次抓包由MCP开始时可执行"
                          "（人工网页开始的会返回错误）。与人工标记（目录改名）"
                          "语义不同")
    async def note_capture(
        text: str = Field(..., description="注释内容（做什么操作/预期触发"
                                          "什么，最多200字）"),
    ) -> dict:
        ok, msg = await capture.note(text, source="mcp")
        return {"ok": ok, "message": msg}

    @mcp.tool(name="get_capture_filters",
              description="查询抓包过滤条件（会话级）：是否记录前端页面操作"
                          "（DOM操作监听注入）、请求类型/URL后缀/"
                          "content-type三类静态资源过滤开关、需要抓包的网址"
                          "清单、忽略的URI规则")
    async def get_capture_filters() -> dict:
        return dict(capture.capture_conf)

    @mcp.tool(name="set_capture_filters",
              description="修改抓包过滤条件（会话级，即时生效；均可选，"
                          "仅更新给定字段，未给字段保持不变）。domains为"
                          "需要抓包的网址主机名列表（空=全部）；"
                          "uri_rules为忽略的URI规则（整体替换），每项"
                          "{type,value}，type=prefix/equals/contains/suffix"
                          "（URI以value开头/等于/包含/结尾），value为URI"
                          "（路径+查询串，不含域名，以/开头），任一命中即不"
                          "抓包。调用示例：忽略/api开头的请求与/health："
                          "uri_rules=[{type:prefix,value:/api},{type:equals,value:/health}]；"
                          "只抓某网站：domains=[www.example.com]。"
                          "三个*_enabled为静态资源过滤开关；ops_inject_enabled"
                          "为是否记录前端页面操作（抓包中修改即时生效：开启"
                          "对已打开页面补注入，关闭拆除已注入监听、已记录的"
                          "操作保留）；已有记录可用purge_filtered_records删除")
    async def set_capture_filters(
        ops_inject_enabled: bool = Field(
            None, description="是否记录前端页面操作（DOM操作监听注入）。"
                              "抓包中修改即时生效：true=开启（已打开页面"
                              "补注入），false=关闭（拆除已注入监听，已记录"
                              "的操作保留）；未在抓包时为下次开始抓包的"
                              "默认值"),
        suffix_enabled: bool = Field(None, description="按URL后缀过滤静态资源"
                                                      "（true=启用过滤，如 .js/.css；false=不过滤）"),
        content_type_enabled: bool = Field(
            None, description="按返回content-type过滤静态资源（true=启用，"
                              "如 image/、text/css；false=不过滤）"),
        type_enabled: bool = Field(
            None, description="按Chrome请求类型过滤静态资源（true=启用，"
                              "如 Script/Image/Stylesheet；false=不过滤）"),
        domains: list = Field(None, description="需要抓包的网址清单（主机名列表，"
                                                "如 [\"www.example.com\"]；"
                                                "空列表=全部网址都抓包）"),
        uri_rules: list = Field(
            None, description="忽略的URI规则列表（整体替换现有规则）。每项为一个"
                              "对象 {\"type\": 匹配方式, \"value\": URI}，type 取四种："
                              "prefix=URI以value开头、equals=URI等于value、"
                              "contains=URI包含value、suffix=URI以value结尾。"
                              "value 只写URI（路径+查询串，不含域名，以/开头），"
                              "如 \"/api/list?page=1\"。调用示例——忽略 /api 开头的"
                              "全部请求与 /health 接口："
                              "[{\"type\": \"prefix\", \"value\": \"/api\"}, "
                              "{\"type\": \"equals\", \"value\": \"/health\"}]。"
                              "任一规则命中的请求不抓包"),
    ) -> dict:
        updates = {k: v for k, v in {
            "suffix_enabled": suffix_enabled,
            "content_type_enabled": content_type_enabled,
            "type_enabled": type_enabled,
            "domains": domains,
            "uri_rules": uri_rules}.items() if v is not None}
        if ops_inject_enabled is None and not updates:
            return {"ok": False, "error": "未提供任何过滤条件字段"}
        ok, err = capture.check_source("mcp")
        if not ok:
            return {"ok": False, "error": err}
        if updates:
            capture.capture_conf.update(updates)
        if ops_inject_enabled is not None:
            ok, err = await capture.set_ops_inject(ops_inject_enabled,
                                                   source="mcp")
            if not ok:
                return {"ok": False, "error": err}
        return {"ok": True, "capture_conf": dict(capture.capture_conf)}

    @mcp.tool(name="purge_filtered_records",
              description="删除被过滤条件命中的抓包记录（页面显示与保存文件"
                          "同步删除，序号不变）。返回删除与保留的记录数量。"
                          "session 为空时对当前正在抓包的会话执行；非空时"
                          "对指定子目录（已结束的会话）执行")
    async def purge_filtered_records(
        session: str = Field(
            "", description="需要删除被过滤记录的抓包记录子目录名称"
                            "（子目录名，不含路径）。为空时对当前正在抓包"
                            "的会话执行；非空时对指定子目录（已结束的会话，"
                            "可用 list_capture_sessions 查询子目录名）执行，"
                            "正在抓包中的目录不允许"),
    ) -> dict:
        if session:
            p = session_path(session)
            if not p:
                return {"ok": False,
                        "error": "抓包记录子目录不存在: %s" % session}
            if (capture.state == "capturing"
                    and session == capture.session_name):
                return {"ok": False, "error": "正在抓包中的目录不允许删除记录"}
            deleted, kept = await asyncio.to_thread(
                purge_history_session, p, session,
                dict(capture.capture_conf), config)
            return {"ok": True, "deleted": deleted, "kept": kept,
                    "message": "已删除 %d 条被过滤的记录（如静态资源），"
                               "保留 %d 条" % (deleted, kept)}
        ok, msg = await capture.purge()
        return {"ok": ok, "message": msg}

    @mcp.tool(name="get_capture_status",
              description="查询当前抓包访问信息：是否正在抓包、抓包文件保存"
                          "目录路径（未抓包为空）、触发方式（web=人工网页/"
                          "mcp=AI Agent，未抓包为空）、data_note（数据读取"
                          "说明：抓包中与结束后分别从哪个文件分析、行序语义）")
    async def get_capture_status() -> dict:
        st = await asyncio.to_thread(capture.status)
        if capture.session_dir and st["state"] in ("capturing", "paused"):
            note = ("抓包中：会话目录实时写入——index.md 为汇总（行序为"
                    "到达顺序，分析时按'请求时间'列排序），明细文件按"
                    "序号命名：请求 {0000000001}.md、前端操作 "
                    "{fr00000001}.md；结束抓包后 index.md 会按时间全量"
                    "重排")
        elif capture.session_dir:
            note = ("会话已结束：index.md 已按时间排序（请求行与前端"
                    "操作行统一时间线），明细文件按序号命名：请求 "
                    "{0000000001}.md、前端操作 {fr00000001}.md；可用 "
                    "rename_capture_session 重命名目录便于识别")
        else:
            note = ""
        return {
            "capturing": st["state"] == "capturing",
            "state": st["state"],
            "session_dir": capture.session_dir or "",
            "trigger": capture.trigger or "",
            "data_note": note,
        }

    @mcp.tool(name="get_capture_hosts",
              description="查询抓包Chrome有访问的网址：自服务启动以来累计"
                          "访问过的域名清单（跨抓包会话累积，不随会话重置），"
                          "按首次访问顺序无关的字母序返回")
    async def get_capture_hosts() -> dict:
        return {"hosts": sorted(capture.seen_hosts),
                "count": len(capture.seen_hosts)}

    @mcp.tool(name="clear_capture_hosts",
              description="清除抓包Chrome访问过的网址：清空累计访问过的"
                          "域名清单；清空后如配置了按网址过滤会回到全部"
                          "网址都抓包（全选/不过滤）")
    async def clear_capture_hosts() -> dict:
        n = await asyncio.to_thread(capture.clear_hosts)
        return {"ok": True, "cleared": n}

    @mcp.tool(name="list_capture_sessions",
              description="查询抓包目录下的子目录：返回抓包记录目录下的全部"
                          "子目录（按目录名倒序、新在前），各含子目录名称、"
                          "记录数、完整路径、是否人工标记；并返回当前正在"
                          "抓包的子目录名称（未抓包为空）")
    async def list_capture_sessions() -> dict:
        items = await asyncio.to_thread(list_history_sessions)
        return {"count": len(items),
                "sessions": [i["name"] for i in items],
                "items": items,
                "active": capture.session_name
                if capture.state == "capturing" else None}

    # ---------- Chrome 插件安装（需求：以上功能都要实现HTTP接口，供AI调用） ----------

    @mcp.tool(name="registry_install_extension",
              description="通过注册表安装chrome插件（策略强装）：打包CRX、写更新源、"
                          "写ExtensionInstallForcelist策略。写策略需管理员，会弹出UAC"
                          "确认框需人工点击（等待约15秒，未确认则失败可重试）。安装后"
                          "自动操作Chrome在chrome://policy点'重新加载政策'使插件立即"
                          "安装（约10秒，期间人工勿动键鼠；Chrome未运行时下次启动"
                          "生效）；安装后无法通过Chrome管理扩展程序页面卸载及管理，"
                          "需用registry_uninstall_extension卸载。更新源由本服务提供，"
                          "需保持服务运行")
    async def registry_install_extension() -> dict:
        from . import ext_install, screen_border
        from .config import EXTENSION_DIR
        # 统一要求：MCP 直调不经后台任务，此处同样显示绿色边框并在
        # 完成后结束 + 通知用户（右下角弹窗）
        screen_border.show()
        try:
            ok, msg = await asyncio.to_thread(
                ext_install.registry_install_run, EXTENSION_DIR,
                config.get("port"))
            ext_install.notify_result("registry_install", ok, msg)
            return {"ok": ok, "message": msg}
        except Exception as e:
            ext_install.notify_result("registry_install", False, str(e))
            return {"ok": False, "error": str(e)}
        finally:
            screen_border.hide()

    @mcp.tool(name="registry_uninstall_extension",
              description="通过注册表卸载chrome插件：删除策略（会弹UAC确认框需人工"
                          "点击）、程序操作Chrome在chrome://policy点'重新加载政策'使"
                          "删除立即生效（约10秒，期间人工勿动键鼠；失败时退回等待约"
                          "3分钟周期刷新）、程序操作Chrome自动移除扩展，全程约1~4"
                          "分钟。后台任务，启动后用get_extension_install_status查询"
                          "进度")
    async def registry_uninstall_extension() -> dict:
        from . import ext_install
        if not ext_install.start_uninstall_task(
                ext_install.registry_ext_id()):
            return {"ok": False, "error": "已有安装/卸载任务在进行"}
        return {"ok": True, "started": True,
                "message": "卸载任务已启动，用 get_extension_install_status 查询进度"}

    @mcp.tool(name="ui_install_extension",
              description="模拟点击安装chrome插件（UI自动化，约10秒）：程序会"
                          "操作日常Chrome在chrome://extensions完成加载未打包的扩展"
                          "程序，操作过程中人工不要操作鼠标与键盘。后台任务，启动后用"
                          "get_extension_install_status查询进度")
    async def ui_install_extension() -> dict:
        from . import ext_install
        from .config import EXTENSION_DIR
        if not ext_install.start_ui_install_task(EXTENSION_DIR):
            return {"ok": False, "error": "已有安装/卸载任务在进行"}
        return {"ok": True, "started": True,
                "message": "安装任务已启动，用 get_extension_install_status 查询进度"}

    @mcp.tool(name="get_extension_install_status",
              description="查询chrome插件安装/卸载后台任务状态：是否运行中、完成结果、"
                          "错误与执行日志（尾部30条）")
    async def get_extension_install_status() -> dict:
        from . import ext_install
        st = ext_install.task.status()
        st["deps"] = await asyncio.to_thread(ext_install.deps_check)
        return st

    # ---------- 配置参数 ----------

    @mcp.tool(name="get_config",
              description="查询Python项目配置参数：监听端口、CDP调试端口、"
                          "静态资源过滤清单、脚本执行超时、开机自启动状态")
    async def get_config() -> dict:
        d = config.as_dict()
        d["auto_start"] = config_mod.get_auto_start()
        return d

    @mcp.tool(name="set_config",
              description="修改Python项目配置参数（均可选，仅更新给定字段）。"
                          "注意：监听端口与CDP端口修改后需重启Python服务生效")
    async def set_config(
        port: int = Field(None, description="监听端口（默认33445）"),
        cdp_port: int = Field(None, description="Chrome CDP调试端口（默认9222）"),
        exec_timeout_sec: int = Field(
            None, description="生成的脚本执行超时秒数（默认300）"),
        suffix_filter: list = Field(None, description="URL后缀过滤清单"),
        content_type_filter: list = Field(None, description="content-type过滤清单"),
        type_filter: list = Field(None, description="Chrome资源类型过滤清单"),
        auto_start: bool = Field(None, description="是否开机自启动（注册表）"),
    ) -> dict:
        updates = {k: v for k, v in {
            "port": port, "cdp_port": cdp_port,
            "exec_timeout_sec": exec_timeout_sec,
            "suffix_filter": suffix_filter,
            "content_type_filter": content_type_filter,
            "type_filter": type_filter,
            "auto_start": auto_start}.items() if v is not None}
        if not updates:
            return {"ok": False, "error": "未提供任何配置字段"}
        config.update(**updates)
        return {"ok": True, "config": await get_config()}

    # ---------- 生成的脚本 ----------

    @mcp.tool(name="list_scripts",
              description="查询生成的python脚本：返回示例目录（python_scripts_example）"
                          "与自定义生成的脚本保存目录下的所有子目录及各子目录下"
                          "的文件名。dirs 为空返回全部子目录，非空仅返回指定"
                          "子目录；"
                          "include_files 控制是否返回子目录中的文件名"
                          "（默认返回）")
    async def list_scripts(
        dirs: list = Field(
            [], description="需要返回的子目录名称列表（空=全部）"),
        include_files: bool = Field(
            True, description="是否需要返回子目录中的全部文件名"
                              "（默认true；false时仅返回子目录名与类别）"),
    ) -> dict:
        from .executor import list_scripts as _ls
        items = _ls()
        if dirs:
            want = {str(d) for d in dirs}
            items = [i for i in items if i["dir"].replace("\\", "/").rstrip(
                "/").split("/")[-1] in want]
        return {"count": len(items),
                "scripts": [{"dir": i["dir"], "category": i["category"],
                             "py_files": [f["path"] for f in i["py_files"]],
                             "doc_files": [f["path"] for f in i["doc_files"]],
                             "readme": i["readme"] or ""} for i in items]
                if include_files else
                [{"dir": i["dir"], "category": i["category"]}
                 for i in items]}

    @mcp.tool(name="run_script",
              description="执行生成的python脚本（同步等待结束，超时自动终止"
                          "进程）。返回是否超时、exit code、stdout/stderr"
                          "（截尾保留）与耗时。路径必须在生成的脚本目录内。"
                          "timeout 非空覆盖配置的超时秒数，空则用参数配置"
                          "页的超时值")
    async def run_script(
        script_path: str = Field(..., description="需要执行的python脚本"
                                                  "文件路径（必填）"),
        max_lines: int = Field(
            100, description="输出数据最大行数（默认100，超过后只输出最后的）"),
        max_bytes: int = Field(
            8000, description="输出数据最大字节数（默认8000，超过后只输出"
                              "最后的）"),
        timeout: int = Field(
            None, description="超时时间秒数：非空覆盖参数配置的超时值，"
                              "空则使用参数配置的值（默认300）"),
    ) -> dict:
        r = await executor.run_to_completion(script_path, timeout=timeout)
        if "error" in r and "exit_code" not in r:
            return r
        r = dict(r)
        r["stdout"] = clip_output(r.get("stdout", ""), max_lines, max_bytes)
        r["stderr"] = clip_output(r.get("stderr", ""), max_lines, max_bytes)
        return r

    # ---------- Cookie ----------

    @mcp.tool(name="query_cookies",
              description="查询接收到的Chrome Cookie：按浏览器语义返回访问"
                          "该主机时会携带的所有Cookie（domain匹配主域+子域），"
                          "及该主机的Authorization。profile 为空则查询全部"
                          "profile的，非空则查询指定profile的（多开区分）")
    async def query_cookies(
        url: str = Field(..., description="目标URL或域名/IP（必填）"),
        profile: int = Field(
            None, description="Chrome实例编号：空则查询全部profile的，"
                              "非空则查询指定profile的"),
    ) -> dict:
        cookies, err = store.query(url, profile or 0)
        auth = store.query_auth(url, profile or 0)
        from app.cookie_query_log import log_cookie_query
        if err and auth is None:
            log_cookie_query("mcp", url, "", ok=False,
                             scope="profile=%d" % (profile or 0), error=err)
            return {"ok": False, "error": err}
        from app.cookie_store import CookieStore
        host = str(url or "").strip()
        if "://" in host:
            from urllib.parse import urlsplit as _us
            host = _us(host).hostname or ""
        else:
            host = host.split("/")[0].split(":")[0]
        log_cookie_query("mcp", url, host.lower(), ok=True,
                         count=len(cookies or []),
                         keys=[c["name"] for c in (cookies or [])],
                         auth_hit=bool(auth),
                         scope="profile=%d" % (profile or 0))
        return {
            "ok": True,
            "count": len(cookies or []),
            "keys": [c["name"] for c in (cookies or [])],
            "cookie_header": CookieStore.cookie_header(cookies or []),
            "authorization": auth or "",
        }

    return mcp
