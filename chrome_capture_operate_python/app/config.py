"""配置管理：conf/conf.json 读写 + Windows 注册表自启动。

conf.json 字段：
- port                监听端口（HTTP + SSE + WebSocket 同端口，URI 区分），默认 33445
- auto_start          是否开机自启动（注册表 HKCU Run），默认否
- suffix_filter       URL 后缀过滤清单（静态资源），即时生效
- content_type_filter 返回 content-type 过滤清单（静态资源），即时生效
- cdp_port            Chrome CDP 调试端口，默认 9222
- exec_timeout_sec    固化脚本执行超时（秒），默认 300
"""
import json
import os
import sys
import threading

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
    "auto_start": False,
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
        """仅允许更新已知字段。auto_start 变化时同步注册表。"""
        with self._lock:
            for k, v in kwargs.items():
                if k in DEFAULTS:
                    self._data[k] = v
        self.save()
        if "auto_start" in kwargs:
            set_auto_start(bool(kwargs["auto_start"]))


def _autostart_cmd():
    pythonw = os.path.join(BASE_DIR, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return '"%s" -m app.main' % pythonw


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


def ensure_dirs():
    for d in (CONF_DIR, LOG_DIR, CAPTURE_DIR, SCRIPTS_EXAMPLE_DIR):
        os.makedirs(d, exist_ok=True)
