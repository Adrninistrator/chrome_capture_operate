"""Chrome 进程管理：注册表查找安装路径、profile 拷贝、启动调试端口实例。"""
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import urllib.request
import winreg

log = logging.getLogger("app.chrome")

PROFILE_DIR = os.path.join(tempfile.gettempdir(), "chrome_capture_operate_profile")
# 收藏夹、访问记录、保存的用户名（密码不可移植，见 docs/design.md）
PROFILE_FILES = ["Bookmarks", "History", "Login Data"]


def find_chrome():
    """通过注册表查找 Chrome 安装路径，失败再试常见路径。"""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
            ) as key:
                path, _ = winreg.QueryValueEx(key, "")
                if path and os.path.exists(path):
                    return path
        except OSError:
            continue
    for p in (
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ):
        if os.path.exists(p):
            return p
    return None


def copy_profile(dst_root=None):
    """把日常 Chrome profile 的收藏夹/历史/用户名拷贝到抓包 profile。

    dst_root 为 None 时用默认抓包 profile 目录；多开实例创建时
    传入实例的数据存储目录（copy_profile_to 的兼容包装）。
    按修改时间+大小跳过未变化文件；单个文件拷贝失败（如被锁定）则跳过。
    """
    return copy_profile_to(PROFILE_DIR if dst_root is None else dst_root)


def copy_profile_to(dst_root):
    """把日常 Chrome profile 数据拷贝到指定数据存储目录（多开实例创建）。"""
    src_dir = os.path.expandvars(
        r"%LocalAppData%\Google\Chrome\User Data\Default")
    dst_dir = os.path.join(dst_root, "Default")
    os.makedirs(dst_dir, exist_ok=True)
    copied, skipped, failed = 0, 0, 0
    for name in PROFILE_FILES:
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        if not os.path.exists(src):
            continue
        try:
            if (os.path.exists(dst)
                    and os.path.getsize(src) == os.path.getsize(dst)
                    and int(os.path.getmtime(src)) == int(os.path.getmtime(dst))):
                skipped += 1
                continue
            shutil.copy2(src, dst)
            copied += 1
        except OSError as e:
            failed += 1
            log.warning("profile 文件拷贝失败，跳过: %s (%s)", name, e)
    log.info("profile 拷贝完成: copied=%d skipped=%d failed=%d",
             copied, skipped, failed)
    return {"copied": copied, "skipped": skipped, "failed": failed}


# CDP 存活探测结果缓存：探测是同步 HTTP 请求（Chrome 未运行时要等
# TCP 连接失败的全程，本机回环下可达数秒），而 /api/status 每次轮询都
# 调它——不缓存会周期性阻塞整个事件循环（所有接口卡顿）。缓存窗口
# 内直接复用上次结果，端口死活变化最多延迟一个轮询周期感知。
_CDP_ALIVE_CACHE = {}  # port -> (monotonic_ts, alive)
_CDP_ALIVE_TTL = 4.0   # 秒；小于前端 5 秒轮询间隔，保证每轮至多探测一次


def is_cdp_alive(cdp_port, force=False):
    """对应端口的调试 Chrome 是否已启动（结果缓存 TTL 秒；force 跳过缓存）。

    force=True 用于"立即需要准确结果"的入口（如点按钮启动 Chrome 前、
    开始抓包前），日常轮询走缓存即可。"""
    import time as _time
    now = _time.monotonic()
    hit = _CDP_ALIVE_CACHE.get(cdp_port)
    if not force and hit and now - hit[0] < _CDP_ALIVE_TTL:
        return hit[1]
    alive = _probe_cdp(cdp_port)
    _CDP_ALIVE_CACHE[cdp_port] = (now, alive)
    return alive


def _probe_cdp(cdp_port):
    """真实的同步 HTTP 探测（可能阻塞数秒，勿在事件循环直接调用）。

    先用短超时（200ms）做 TCP 连通预检：本机回环上端口未监听时，
    连接被拒绝是毫秒级——但部分环境（防火墙对进程的出站规则）会把
    SYN 直接丢弃，connect 会挂满整个超时。预检失败直接判死，避免
    稀释成 HTTP 层的长等待；预检通过再用原 HTTP 探测拿状态码。"""
    try:
        s = socket.create_connection(("127.0.0.1", cdp_port), timeout=0.2)
        s.close()
    except OSError:
        return False  # 端口无监听（拒绝或被丢弃），无需发 HTTP
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % cdp_port, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# "变体扩展"（复制项目扩展注入 FORCED_PROFILE 常量供多开自动对号）已
# 随编号方案回退移除：新版 background.js 不再读取 FORCED_PROFILE，注入
# 的常量是死代码；多开编号由人工在插件"参数配置"页设置（storage 的
# my_profile，见 docs/design-profile-id.md 的实测结论与最终方案）。


def start_chrome_with_profile(user_data_dir, cdp_port=None):
    """以指定数据存储目录启动一个 Chrome 实例（多开）。

    cdp_port 为 None 时不带调试端口（多开实例仅登录/供 Cookie，不抓包）。
    返回 (ok, message)。不检查端口占用。
    """
    chrome = find_chrome()
    if not chrome:
        return False, "未找到 Chrome 安装路径（注册表与常见路径均未命中）"
    args = [
        chrome,
        "--user-data-dir=%s" % user_data_dir,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
    ]
    if cdp_port:
        args.append("--remote-debugging-port=%d" % cdp_port)
    try:
        subprocess.Popen(
            args,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as e:
        return False, "启动 Chrome 失败: %s" % e
    return True, "Chrome 已启动（数据目录 %s）" % user_data_dir


def start_capture_chrome(cdp_port):
    """启动用于抓包的 Chrome。已启动则不重复启动。返回 (ok, message)。"""
    if is_cdp_alive(cdp_port, force=True):
        return True, "调试端口 %d 的 Chrome 已在运行，未重复启动" % cdp_port
    chrome = find_chrome()
    if not chrome:
        return False, "未找到 Chrome 安装路径（注册表与常见路径均未命中）"
    copy_profile()
    args = [
        chrome,
        "--remote-debugging-port=%d" % cdp_port,
        "--user-data-dir=%s" % PROFILE_DIR,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
    ]
    try:
        subprocess.Popen(
            args,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as e:
        return False, "启动 Chrome 失败: %s" % e
    # 等待 CDP 端口就绪（最多 15 秒）——刚拉起必须真实探测（force）
    import time
    for _ in range(30):
        if is_cdp_alive(cdp_port, force=True):
            return True, "Chrome 已启动，CDP 端口 %d" % cdp_port
        time.sleep(0.5)
    return False, "Chrome 进程已拉起但 CDP 端口 %d 未就绪（可能被占用）" % cdp_port


def chrome_version(cdp_port):
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % cdp_port, timeout=2) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
