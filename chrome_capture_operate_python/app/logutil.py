"""日志：写到 log 目录、每天一个文件；Cookie/set-cookie/Authorization 值不落盘。

同时输出到 stderr（PyCharm/控制台启动调试时需要）；pythonw 下 sys.stderr 为
None，此时跳过控制台输出，避免 StreamHandler 写 None 抛异常。
"""
import logging
import os
import re
import sys
from datetime import datetime

from .config import LOG_DIR

_AUTH = re.compile(
    r"(?i)(authorization)(['\"]?\s*[:=]\s*['\"]?)([^'\",\n}]+)")
_COOKIE = re.compile(
    r"(?i)(cookie|set-cookie)(['\"]?\s*[:=]\s*['\"]?)([^'\",\n}]+)")


def _mask_cookie(m):
    """cookie/set-cookie 掩码（与抓包文件掩码同规则）：

    - cookie（请求头/kv 形态）：各 pair 值掩码、key 保留；
    - set-cookie：仅首个 pair（cookie 本体）值掩码，Path/Expires 等属性保留；
    - 裸值（cookie=abc 形态）：整体掩码。
    """
    name = m.group(1).lower()
    out = []
    for i, seg in enumerate(m.group(3).split(";")):
        s = seg.strip()
        if "=" in s and (name == "cookie" or i == 0):
            out.append(s.split("=", 1)[0].strip() + "=***")
        elif i == 0 and "=" not in s:
            out.append("***")
        else:
            out.append(s)
    return m.group(1) + m.group(2) + "; ".join(out)


def redact(text):
    """authorization 整值掩码；cookie/set-cookie 保留 key、值掩码。"""
    if not isinstance(text, str):
        text = str(text)
    text = _AUTH.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    return _COOKIE.sub(_mask_cookie, text)


class RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, datetime.now().strftime("%Y-%m-%d") + ".log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(fmt)
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.addFilter(RedactFilter())
        root.addHandler(console)
    return logging.getLogger("app"), path


def uvicorn_log_config(log_path):
    """uvicorn 的日志配置：写同一日志文件并脱敏；有控制台时同步输出。

    不能用 uvicorn 默认配置——pythonw 下 sys.stderr 为 None，
    默认 StreamHandler 可能在输出时抛异常。
    """
    handlers = {"file": {
        "class": "logging.FileHandler", "filename": log_path,
        "encoding": "utf-8", "formatter": "default",
        "filters": ["redact"]}}
    use = ["file"]
    if sys.stderr is not None:
        handlers["console"] = {
            "class": "logging.StreamHandler", "formatter": "default",
            "filters": ["redact"], "stream": "ext://sys.stderr"}
        use.append("console")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"redact": {"()": "app.logutil.RedactFilter"}},
        "formatters": {"default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"}},
        "handlers": handlers,
        "loggers": {
            "uvicorn": {"handlers": use, "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": use, "level": "INFO",
                              "propagate": False},
            "uvicorn.access": {"handlers": use, "level": "INFO",
                               "propagate": False},
        },
    }
