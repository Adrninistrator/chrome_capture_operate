"""Cookie 查询审计：结构化日志文件（每天滚动）+ 页面展示当天最新 50 条。

安全要求（2026-09-28）：查询登录态（Cookie/Authorization）属敏感操作，
逐次留痕——来源、目标地址、返回规模（cookie 数量与 key、Authorization
有无），不记录任何值。三个查询入口共用：HTTP /api/cookies/query
（日常 Chrome 插件快照）、HTTP /api/cookies/cdp（抓包 Chrome 实时
读取）、MCP query_cookies 工具。

结构化日志（2026-09-29）：log/cookie_query.log 每行一条 JSON（字段即
log_cookie_query 的 record dict），TimedRotatingFileHandler 每天午夜
滚动（滚动文件名 cookie_query.log.YYYY-MM-DD，不自动删除——审计记录
保留全部）。页面"Chrome Cookie接收与查询记录"的查询记录从当天日志
文件读取最新 50 条展示（服务重启不丢；仅当天——更早记录人工直接
查看日志文件）。字段名刻意避开 authorization/cookie 字样（用 auth_hit
等）：旧文本格式的"Authorization=有"会被 RedactFilter 整值掩码成
"Authorization=***"，审计日志反而看不出有没有 Authorization。
"""
import json
import logging
import os
import threading
import time

from . import config
from .logutil import RedactFilter

# 查询来源标签（页面展示与日志共用）
SOURCE_LABELS = {
    "web": "日常Chrome（HTTP）",
    "cdp": "抓包Chrome（CDP）",
    "mcp": "MCP工具",
}

PAGE_LIMIT = 50  # 页面展示的当天最新条数

_query_logger = logging.getLogger("app.cookiequery")
_lock = threading.Lock()
_file_handler = None       # 显式引用（caplog 会注入临时 handler，不能以
                           # handlers 非空作为已初始化判据）


def today():
    return time.strftime("%Y-%m-%d")


def log_path(day=None):
    """指定日期（YYYY-MM-DD，默认当天）的查询日志文件路径。

    TimedRotatingFileHandler 滚动后，历史内容在 cookie_query.log.YYYY-MM-DD，
    cookie_query.log 始终是当天文件。"""
    day = day or today()
    name = "cookie_query.log" if day == today() else \
        "cookie_query.log.%s" % day
    return os.path.join(config.LOG_DIR, name)


def _setup_logger():
    """初始化独立日志（首次记录时惰性执行）：结构化 JSON 行 + 每天滚动 + 脱敏。

    LOG_DIR 动态读取（config.LOG_DIR），测试可重定向后再 reset。"""
    global _file_handler
    if _file_handler is not None:
        return
    os.makedirs(config.LOG_DIR, exist_ok=True)
    from logging.handlers import TimedRotatingFileHandler
    handler = TimedRotatingFileHandler(
        log_path(), when="midnight", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))  # 消息本身即 JSON 行
    handler.addFilter(RedactFilter())     # 双保险：url/error 字段即使误含值也脱敏
    _query_logger.setLevel(logging.INFO)
    _query_logger.addHandler(handler)
    _query_logger.propagate = False       # 不进主日志（主日志另有概要行）
    _file_handler = handler


def log_cookie_query(source, url, host, ok, count=0, keys=None,
                     auth_hit=False, scope="", error=""):
    """记录一次 Cookie 查询：向结构化日志文件追加一行 JSON。

    source：web / cdp / mcp（见 SOURCE_LABELS）；url 为原始请求参数，
    host 为解析出的主机名；count 与 keys 为返回的 cookie 数量与 key
    列表（不记录值）；auth_hit 表示 Authorization 是否命中；scope 为
    profile 编号（query）或 CDP 端口（cdp）的描述字符串；error 为
    失败原因（成功为空）。
    """
    _setup_logger()
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "url": str(url or "")[:200],
        "host": str(host or "")[:100],
        "scope": str(scope or ""),
        "count": int(count or 0),
        "keys": sorted({str(k) for k in (keys or []) if str(k)})[:80],
        "auth_hit": bool(auth_hit),
        "success": bool(ok),
        "error": str(error or "")[:160],
    }
    # 每行一条 JSON（ensure_ascii=False 保留中文，便于直接阅读日志）
    _query_logger.info(json.dumps(record, ensure_ascii=False))


def queries(day=None):
    """读取指定日期（默认当天）的查询记录：最新在前，最多 PAGE_LIMIT 条。

    从结构化日志文件读取（服务重启后记录不丢）；按记录时间过滤日期——
    跨零点未发生写入时当天文件可能仍含昨日内容（处理器首次写入才滚动），
    过滤保证严格"当天"。无法解析为 JSON 的行（结构化改造前的旧文本
    格式）跳过。文件不存在返回空列表。
    """
    day = day or today()
    records = []
    try:
        with open(log_path(day), "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(rec, dict)
                        and str(rec.get("time", "")).startswith(day)):
                    records.append(rec)
    except OSError:
        return []
    return records[-PAGE_LIMIT:][::-1]


def reset_for_test():
    """测试隔离：移除文件 handler（配合 LOG_DIR 重定向）。"""
    global _file_handler
    if _file_handler is not None:
        _query_logger.removeHandler(_file_handler)
        try:
            _file_handler.close()
        except Exception:
            pass
        _file_handler = None
