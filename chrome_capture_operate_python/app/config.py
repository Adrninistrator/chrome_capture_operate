"""配置管理：conf/conf.json 读写 + Windows 注册表自启动。

conf.json 字段：
- port                监听端口（HTTP + SSE + WebSocket 同端口，URI 区分），默认 33445
- suffix_filter       URL 后缀过滤清单（静态资源），即时生效
- content_type_filter 返回 content-type 过滤清单（静态资源），即时生效
- cdp_port            Chrome CDP 调试端口，默认 9222
- exec_timeout_sec    固化脚本执行超时（秒），默认 300

auto_start（是否开机自启动，默认否）：参数值按需求写入全局配置文件
global_conf.json（见 app/globalconf.py，与项目目录解耦），注册表 HKCU Run
为落地实现，启动时按参数值同步（ensure_auto_start）。
"""
import json
import logging
import os
import re
import sys
import threading

from . import globalconf

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # chrome_capture_operate_python
PROJECT_ROOT = os.path.dirname(BASE_DIR)                                  # chrome_capture_operate
CONF_DIR = os.path.join(BASE_DIR, "conf")
CONF_PATH = os.path.join(CONF_DIR, "conf.json")
LOG_DIR = os.path.join(BASE_DIR, "log")
CAPTURE_DIR = os.path.join(BASE_DIR, "captured_record")
# 示例脚本根目录（快速执行页"示例"类）；用户自定义脚本根目录在全局配置
# global_conf.json 的 python_scripts_dir_path 中配置
SCRIPTS_EXAMPLE_DIR = os.path.join(BASE_DIR, "python_scripts_example")
# 定时执行脚本的日志根目录：log/scripts/{子目录名}/{年份}/{脚本名}_{执行时间}.log
# （定时任务本身保存在全局配置文件，见 app/globalconf.py KEY_SCHEDULES）
SCRIPT_LOG_DIR = os.path.join(LOG_DIR, "scripts")
API_DOC_PATH = os.path.join(PROJECT_ROOT, "api", "api.md")
# Chrome 插件目录（页头"安装Chrome插件"按钮复制给用户去加载）
EXTENSION_DIR = os.path.join(PROJECT_ROOT, "chrome_capture_operate_extension")

DEFAULT_SUFFIX_FILTER = [
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".bmp", ".webp", ".avif",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".map", ".vtt",
    ".mp4", ".webm", ".mp3", ".wav",
    ".m4s", ".m3u8", ".mpd",  # 流媒体分段/播放列表（HLS/DASH）
]
# 注意：application/octet-stream 常见于真实 API（protobuf/文件下载），不能过滤
# .ts 不加入后缀清单：与 TypeScript 扩展名冲突，HLS ts 分片的 video/mp2t
# 已被 video/ 前缀覆盖
DEFAULT_CONTENT_TYPE_FILTER = [
    "text/css", "image/", "font/", "audio/", "video/",
    "application/javascript", "application/x-javascript", "text/javascript",
    "application/font", "application/x-font", "application/vnd.ms-fontobject",
    "text/vtt",
    # 流媒体播放列表（hls.js 等以 XHR 拉流，类型过滤拦不到，靠此路兜底）
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "application/dash+xml",
]
# Chrome 资源类型过滤清单（CDP params.type）：只含纯静态资源。
# Document(网页)/XHR/Fetch 一律保留——业务接口绝不被误杀
DEFAULT_TYPE_FILTER = [
    "Stylesheet", "Script", "Image", "Font", "Media",
    "TextTrack", "Manifest", "Prefetch",
]

DEFAULTS = {
    "port": 33445,
    "suffix_filter": DEFAULT_SUFFIX_FILTER,
    "content_type_filter": DEFAULT_CONTENT_TYPE_FILTER,
    "type_filter": DEFAULT_TYPE_FILTER,
    "cdp_port": 9222,
    "exec_timeout_sec": 300,
}

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "chrome_capture_operate"


class Config:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        os.makedirs(CONF_DIR, exist_ok=True)
        if os.path.exists(CONF_PATH):
            try:
                with open(CONF_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k in DEFAULTS:
                    if k in saved:
                        self._data[k] = saved[k]
            except Exception:
                pass  # 配置文件损坏时使用默认值

    def save(self):
        os.makedirs(CONF_DIR, exist_ok=True)
        with self._lock:
            data = dict(self._data)
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def as_dict(self):
        with self._lock:
            return dict(self._data)

    def update(self, **kwargs):
        """仅允许更新已知字段。

        auto_start 不进 conf.json：参数值写全局配置文件，并同步注册表。
        返回 None（无 auto_start）或 {"auto_start_registry_ok": bool}。
        """
        with self._lock:
            for k, v in kwargs.items():
                if k in DEFAULTS:
                    self._data[k] = v
        self.save()
        if "auto_start" in kwargs:
            v = bool(kwargs["auto_start"])
            globalconf.set_value(globalconf.KEY_AUTO_START,
                                 "true" if v else "false")
            return {"auto_start_registry_ok": set_auto_start(v)}
        return None


def _pythonw():
    """优先 .venv 的 pythonw；否则找当前解释器同目录的 pythonw.exe；
    都没有则退回 sys.executable（可能带控制台窗口，但能用）。"""
    pythonw = os.path.join(BASE_DIR, ".venv", "Scripts", "pythonw.exe")
    if os.path.exists(pythonw):
        return pythonw
    exe = sys.executable
    if os.path.basename(exe).lower() != "pythonw.exe":
        sibling = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(sibling):
            return sibling
    return exe


def _autostart_cmd():
    # 注意：Run 键进程的工作目录是 System32（不是项目目录），不能用
    # `-m app.main`——它依赖当前目录找 app 包，开机时会静默失败。
    # 必须用绝对路径直接运行 main.py 脚本（其 __package__ 补齐逻辑
    # 使脚本方式不依赖工作目录）。
    main_py = os.path.join(BASE_DIR, "app", "main.py")
    return '"%s" "%s"' % (_pythonw(), main_py)


def set_auto_start(enabled):
    """通过注册表 HKCU Run 实现自启动。失败返回 False。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  _autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def get_auto_start():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as key:
            winreg.QueryValueEx(key, _RUN_VALUE_NAME)
            return True
    except OSError:
        return False


def _registry_run_value():
    """读取注册表 Run 键当前命令字符串；无值/异常返回 ""。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as key:
            v, _ = winreg.QueryValueEx(key, _RUN_VALUE_NAME)
            return v if isinstance(v, str) else ""
    except OSError:
        return ""


def _run_value_valid(cmd):
    """注册表命令是否可用：须为两个引号路径（解释器 + main.py）且都存在。

    旧版 `-m app.main` 命令只有一段引号路径（依赖工作目录，开机必失败），
    判为无效，触发重建。指向其他有效副本的命令视为有效（不抢占）。
    """
    paths = re.findall(r'"([^"]+)"', cmd)
    return (len(paths) == 2
            and all(os.path.exists(p) for p in paths))


def _legacy_conf_auto_start():
    """旧版 conf.json 的 auto_start 值；无记录/文件损坏返回 None。"""
    try:
        with open(CONF_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict) and "auto_start" in saved:
            return bool(saved["auto_start"])
    except (OSError, ValueError):
        pass
    return None


def ensure_auto_start():
    """程序启动时同步自启动：全局配置的参数值为准，注册表为落地实现。

    - 已启用：注册表命令缺失 / 路径失效（项目目录被移走、旧 -m 格式）/
      被清理工具误删时，按当前路径重建；指向其他有效副本时尊重不抢
      （同机多副本，最后写开关的副本获得自启动）；
    - 未启用：清除注册表残留（幂等）；
    - 全局无值（旧版升级）：迁移旧 conf.json 的 auto_start，再退到注册表
      现状，迁移结果落盘全局配置。

    返回同步后的启用状态。注册表写入失败仅影响本次，下次启动重试。
    """
    log = logging.getLogger("app")
    v = globalconf.get_value(globalconf.KEY_AUTO_START)
    if v == "":
        legacy = _legacy_conf_auto_start()
        enabled = bool(legacy) if legacy is not None else get_auto_start()
        globalconf.set_value(globalconf.KEY_AUTO_START,
                             "true" if enabled else "false")
        log.info("自启动参数已迁移至全局配置文件: %s", enabled)
    else:
        enabled = v == "true"
    if enabled:
        if not _run_value_valid(_registry_run_value()):
            set_auto_start(True)
            log.info("已按当前路径重建开机自启动注册表命令")
    else:
        set_auto_start(False)
    return enabled


def ensure_dirs():
    for d in (CONF_DIR, LOG_DIR, CAPTURE_DIR, SCRIPTS_EXAMPLE_DIR):
        os.makedirs(d, exist_ok=True)
