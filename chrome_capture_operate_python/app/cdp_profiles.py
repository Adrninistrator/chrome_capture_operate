# -*- coding: utf-8 -*-
"""CDP Chrome 多实例：配置登记、端口规则、按需创建与启动。

设计文档：docs/自动登录脚本/多账号多CDP实例方案.md

与 chrome_multi_profile（插件多开，非 CDP）完全独立：

- 默认抓包实例（默认 9222）自动登记、受保护（禁止删除/改端口），
  继续使用旧 profile 目录（``%TEMP%\\chrome_capture_operate_profile``）；
- 9223–9999 的登录实例由自动登录脚本显式指定端口，按需 ensure 创建；
  profile 目录默认 ``<base>_<port>``，使用空 profile（不复制日常数据）；
- 端口是实例唯一标识，创建后不可变；删除只移除登记，保留 profile 目录，
  同一端口再次 ensure 时接管旧目录（保留登录态）；
- 只有默认抓包实例参与抓包、安装插件、提供 Authorization；登录实例
  只使用 CDP Cookie。

全局配置 ``cdp_chrome_profiles`` 结构：

```json
[
  {"name": "抓包实例", "port": 9222,
   "profile_dir": "C:\\\\...\\\\chrome_capture_operate_profile",
   "auto_created": false, "created": "2026-10-28 10:00:00"},
  {"name": "账号A", "port": 9223,
   "profile_dir": "C:\\\\...\\\\chrome_capture_operate_profile_9223",
   "auto_created": true, "created": "2026-10-28 10:01:00"}
]
```
"""
import logging
import os
import threading
from datetime import datetime

from . import chrome_proc
from . import globalconf

log = logging.getLogger("app.cdp_profiles")

DEFAULT_CAPTURE_PORT = 9222
LOGIN_PORT_MIN = 9223
LOGIN_PORT_MAX = 9999

CAPTURE_NAME = "抓包实例"

# 配置读写的全局锁（短临界区，只保护 cdp_chrome_profiles 的读-改-写）
_LOCK = threading.RLock()

# 按端口的启动锁：同一端口并发 ensure/open 只启动一次；不同端口可并行
_PORT_LOCKS = {}
_PORT_LOCKS_GUARD = threading.Lock()


class CdpProfileError(Exception):
    """可返回给调用方的错误（不包含账号值）。"""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def to_dict(self):
        return {"ok": False, "error": self.message}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _port_lock(port):
    with _PORT_LOCKS_GUARD:
        return _PORT_LOCKS.setdefault(int(port), threading.RLock())


def capture_port_of(port=None):
    """整数化端口；None/非法返回 9222。"""
    try:
        return int(port)
    except (TypeError, ValueError):
        return DEFAULT_CAPTURE_PORT


def is_capture_port(port, capture_port=None):
    return capture_port_of(port) == capture_port_of(capture_port)


def base_profile_dir():
    """旧抓包 profile 目录（默认抓包实例继续使用，不加 _9222 后缀）。"""
    return chrome_proc.PROFILE_DIR


def default_profile_dir(port, capture_port=None):
    """端口 -> 默认 profile 目录。

    抓包实例使用旧目录；登录实例使用 ``<base>_<port>``（空 profile）。
    """
    port = capture_port_of(port)
    if is_capture_port(port, capture_port):
        return base_profile_dir()
    return "%s_%d" % (base_profile_dir(), port)


def default_name(port, capture_port=None):
    port = capture_port_of(port)
    if is_capture_port(port, capture_port):
        return CAPTURE_NAME
    return "CDP实例_%d" % port


def role_of(port, capture_port=None):
    return "capture" if is_capture_port(port, capture_port) else "login"


def _normalize_entry(it, capture_port):
    """配置项 -> 规范结构；非法项返回 None。"""
    if not isinstance(it, dict):
        return None
    try:
        port = int(it.get("port"))
    except (TypeError, ValueError):
        return None
    name = str(it.get("name") or "").strip() \
        or default_name(port, capture_port)
    profile_dir = str(it.get("profile_dir") or "").strip() \
        or default_profile_dir(port, capture_port)
    return {
        "name": name,
        "port": port,
        "profile_dir": profile_dir,
        "auto_created": bool(it.get("auto_created")),
        "created": str(it.get("created") or ""),
    }


def _read_entries(capture_port):
    """读取并规范化全部登记项（不写回）。"""
    raw = globalconf.get_json(globalconf.KEY_CDP_PROFILES, [])
    if not isinstance(raw, list):
        raw = []
    entries = []
    seen = set()
    for it in raw:
        e = _normalize_entry(it, capture_port)
        if e is None or e["port"] in seen:
            continue
        seen.add(e["port"])
        entries.append(e)
    entries.sort(key=lambda x: x["port"])
    return entries


def _capture_entry(capture_port):
    return {
        "name": CAPTURE_NAME,
        "port": capture_port_of(capture_port),
        "profile_dir": default_profile_dir(capture_port, capture_port),
        "auto_created": False,
        "created": _now(),
    }


def _ensure_capture_entry(entries, capture_port):
    """补登记默认抓包实例；返回 (entries, changed)。"""
    cp = capture_port_of(capture_port)
    if any(e["port"] == cp for e in entries):
        return entries, False
    entries.append(_capture_entry(cp))
    entries.sort(key=lambda x: x["port"])
    return entries, True


def _save(entries):
    ok = globalconf.set_json(globalconf.KEY_CDP_PROFILES, entries)
    if not ok:
        raise CdpProfileError("全局配置写入失败", status_code=500)


def _decorate(entry, capture_port, include_alive=False):
    out = dict(entry)
    out["role"] = role_of(entry["port"], capture_port)
    if include_alive:
        out["alive"] = chrome_proc.is_cdp_alive(entry["port"])
    return out


def list_profiles(capture_port=None, include_alive=False):
    """登记项列表（含自动登记的默认抓包实例），并写回缺失的默认项。"""
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    with _LOCK:
        entries = _read_entries(cp)
        entries, changed = _ensure_capture_entry(entries, cp)
        if changed:
            _save(entries)
    return [_decorate(e, cp, include_alive) for e in entries]


def status_profiles(capture_port=None):
    """只读状态（供 /api/status 轮询；不写配置，避免轮询产生副作用）。"""
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    entries = _read_entries(cp)
    entries, _ = _ensure_capture_entry(entries, cp)
    return [_decorate(e, cp, include_alive=True) for e in entries]


def get_profile(port, capture_port=None):
    """按端口查登记项（不含自动登记，尽量只读）；不存在返回 None。"""
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    port = capture_port_of(port)
    for e in _read_entries(cp):
        if e["port"] == port:
            return _decorate(e, cp)
    return None


def is_known_port(port, capture_port=None):
    """正式接口允许的端口：抓包端口或已登记端口。"""
    port = capture_port_of(port)
    if is_capture_port(port, capture_port):
        return True
    return get_profile(port, capture_port) is not None


def _validate_login_port(port, capture_port):
    port = capture_port_of(port)
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    if port == cp:
        return port
    if not (LOGIN_PORT_MIN <= port <= LOGIN_PORT_MAX):
        raise CdpProfileError(
            "端口必须在 %d–%d 之间（默认抓包实例为 %d）"
            % (LOGIN_PORT_MIN, LOGIN_PORT_MAX, cp))
    return port


def _check_port_free(port, allow_alive=False):
    """端口占用检查：不杀进程、不抢占。

    allow_alive=True 只用于默认抓包实例：应用重启后 9222 上的 Chrome
    可能仍在运行，此时直接接管（不重复启动）。
    """
    if chrome_proc.is_cdp_alive(port, force=True):
        if allow_alive:
            return
        raise CdpProfileError(
            "端口 %d 已被未登记的 Chrome 占用，不自动接管（若为已删除实例的"
            " Chrome，请先关闭它再重试；也可更换端口）" % port,
            status_code=409)
    if chrome_proc.is_port_occupied(port):
        raise CdpProfileError(
            "端口 %d 已被其他程序占用，无法启动 CDP Chrome（请更换端口或先关闭占用程序）"
            % port, status_code=409)


def _start_instance(entry, capture_port):
    """启动实例（调用方需持有该端口锁）；返回 (ok, message)。"""
    port = entry["port"]
    if chrome_proc.is_cdp_alive(port, force=True):
        return True, "调试端口 %d 的 Chrome 已在运行，未重复启动" % port
    _check_port_free(port)
    if is_capture_port(port, capture_port):
        # 默认抓包实例保持旧行为：启动前增量拷贝日常 Chrome 的
        # 收藏夹/历史/用户名（登录实例使用空 profile，不复制）
        chrome_proc.copy_profile(entry["profile_dir"])
    return chrome_proc.start_cdp_chrome(port, entry["profile_dir"])


def ensure_profile(port, name="", capture_port=None):
    """按端口 ensure：不存在则自动创建（登录实例），然后启动并等待就绪。

    返回 {"port", "name", "profile_dir", "auto_created", "created_now",
    "role", "message"}。并发调用同一端口只创建一个实例（端口锁 + 幂等）。
    """
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    port = _validate_login_port(port, cp)
    name = str(name or "").strip()

    with _port_lock(port):
        created_now = False
        with _LOCK:
            entries = _read_entries(cp)
            entries, captured = _ensure_capture_entry(entries, cp)
            entry = next((e for e in entries if e["port"] == port), None)
            if entry is None:
                # 端口被其他程序占用时不抢占。默认抓包实例允许接管重启后
                # 仍在运行的旧 Chrome；登录实例在其默认 profile 目录已存在
                # 时也视为“删除登记后同端口再次 ensure”，接管旧实例
                # （保留登录态）；目录不存在且端口有未知 Chrome 则报错。
                allow_alive = (is_capture_port(port, cp)
                               or os.path.isdir(default_profile_dir(port, cp)))
                _check_port_free(port, allow_alive=allow_alive)
                entry = {
                    "name": name or default_name(port, cp),
                    "port": port,
                    "profile_dir": default_profile_dir(port, cp),
                    "auto_created": True,
                    "created": _now(),
                }
                entries.append(entry)
                entries.sort(key=lambda x: x["port"])
                created_now = True
            if captured or created_now:
                _save(entries)
            entry = dict(entry)

        ok, message = _start_instance(entry, cp)
        if not ok:
            raise CdpProfileError(message, status_code=409)
        log.info("CDP 实例就绪: 端口=%d 名称=%s 新建=%s 目录=%s",
                 port, entry["name"], created_now, entry["profile_dir"])
        return {
            "ok": True,
            "message": message,
            "port": port,
            "name": entry["name"],
            "profile_dir": entry["profile_dir"],
            "auto_created": entry["auto_created"],
            "created_now": created_now,
            "role": role_of(port, cp),
        }


def create_profile(name, port, profile_dir=None, capture_port=None):
    """人工新增登录实例（不启动）；端口占位检查不通过时报错。"""
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    name = str(name or "").strip()
    if not name:
        raise CdpProfileError("名称不能为空")
    port = _validate_login_port(port, cp)
    if is_capture_port(port, cp):
        raise CdpProfileError("抓包端口 %d 已自动登记，不能重复新增" % port)

    d = str(profile_dir or "").strip()
    if d:
        if not os.path.isabs(d):
            raise CdpProfileError("数据目录需要完整路径：%s" % d)
        d = os.path.abspath(d)
    else:
        d = default_profile_dir(port, cp)

    with _port_lock(port):
        with _LOCK:
            entries = _read_entries(cp)
            entries, captured = _ensure_capture_entry(entries, cp)
            if any(e["port"] == port for e in entries):
                raise CdpProfileError("端口 %d 的实例已存在" % port)
            norm = os.path.normcase(os.path.normpath(d))
            for e in entries:
                if os.path.normcase(os.path.normpath(e["profile_dir"])) == norm:
                    raise CdpProfileError(
                        "数据目录已被端口 %d 的实例使用：%s"
                        % (e["port"], d))
            _check_port_free(port)
            entry = {
                "name": name,
                "port": port,
                "profile_dir": d,
                "auto_created": False,
                "created": _now(),
            }
            entries.append(entry)
            entries.sort(key=lambda x: x["port"])
            _save(entries)
    log.info("新增 CDP 实例: 端口=%d 名称=%s 目录=%s", port, name, d)
    return _decorate(entry, cp)


def rename_profile(port, name, capture_port=None):
    """修改实例名称（端口不可变）。"""
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    port = capture_port_of(port)
    name = str(name or "").strip()
    if not name:
        raise CdpProfileError("名称不能为空")
    with _LOCK:
        entries = _read_entries(cp)
        entries, _ = _ensure_capture_entry(entries, cp)
        entry = next((e for e in entries if e["port"] == port), None)
        if entry is None:
            raise CdpProfileError("实例不存在", status_code=404)
        entry["name"] = name
        _save(entries)
    log.info("修改 CDP 实例名称: 端口=%d 名称=%s", port, name)
    return _decorate(entry, cp)


def delete_profile(port, capture_port=None):
    """删除登记（保留 profile 目录）；抓包实例禁止删除。"""
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    port = capture_port_of(port)
    if is_capture_port(port, cp):
        raise CdpProfileError("默认抓包实例（端口 %d）不可删除" % cp)
    with _LOCK:
        entries = _read_entries(cp)
        removed = next((e for e in entries if e["port"] == port), None)
        rest = [e for e in entries if e["port"] != port]
        if removed is None:
            raise CdpProfileError("实例不存在", status_code=404)
        _save(rest)
    log.info("删除 CDP 实例登记（profile 目录保留）: 端口=%d", port)
    msg = "已删除登记，profile 目录已保留"
    # 默认目录按 <base>_<port> 规则，同端口再次 ensure 可自动接管；自定义
    # 目录无法从端口推断，需人工重新新增并填写同一目录
    if os.path.normcase(os.path.normpath(removed["profile_dir"])) != \
            os.path.normcase(os.path.normpath(
                default_profile_dir(port, cp))):
        msg += "（该实例使用自定义目录，再次 ensure 不会自动接管，" \
               "请重新新增并填写同一目录）"
    return {"ok": True, "port": port, "message": msg}


def open_profile(port, capture_port=None):
    """启动已登记实例并等待 CDP 就绪。"""
    cp = capture_port_of(capture_port or DEFAULT_CAPTURE_PORT)
    port = capture_port_of(port)
    entry = get_profile(port, cp)
    if entry is None:
        raise CdpProfileError("实例不存在", status_code=404)
    with _port_lock(port):
        ok, message = _start_instance(entry, cp)
    if not ok:
        raise CdpProfileError(message, status_code=409)
    return {"ok": True, "message": message, "port": port,
            "profile_dir": entry["profile_dir"],
            "role": role_of(port, cp)}