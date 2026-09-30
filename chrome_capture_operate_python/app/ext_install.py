# -*- coding: utf-8 -*-
"""Chrome 插件自动安装（改编自 docs 目录已验证的脚本，需求「安装Chrome插件-自动」）。

两种方式（均有 docs 实测依据）：
- 注册表策略安装：本地打包 CRX3 + 本地更新源（由本服务 HTTP 提供）+
  ExtensionInstallForcelist 策略（强装）。策略写入/删除走 **reg 文件 +
  regedit.exe**（需求：UAC 弹出仅限 regedit 执行 reg 文件步骤）。
  策略写入成功后自动操作日常 Chrome 在 chrome://policy 点"重新加载
  政策"（需求：安装及卸载时该步骤都通过 python 程序操作 Chrome 实现），
  策略立即生效、Chrome 即刻从更新源拉取安装——替代人工点击/重启
  Chrome/等约 3 分钟策略周期刷新。
  统一要求（安装完毕让客户知道）：任务完成/失败时在桌面右下角弹出
  30 秒后自动消失的窗口（popup_notify，main.py 注入，Web 页与 AI/MCP
  触发均生效）；注册表安装成功后自动在日常 Chrome 打开 chrome://
  extensions 展示插件卡片（chrome.exe 直开 chrome:// 不可靠，用与
  install_ui 相同的键盘导航）。
  强装后无法通过 chrome://extensions 卸载/管理（企业策略语义），
  卸载 = 删策略（regedit UAC）+ 自动在 chrome://policy 点"重新加载
  政策"使删除立即生效（失败时退回盲等周期刷新）+ UI 自动化点"移除"。
- 模拟点击安装：pywinauto UI 自动化（win32 置焦/键盘 + UIA 查
  网页按钮与对话框），驱动已运行的日常 Chrome 在 chrome://extensions
  完成"加载未打包的扩展程序"。运行期间人工不要操作鼠标与键盘。

实测结论（见 docs/chrome插件注册表策略安装/README.md、
docs/chrome插件自动安装UI自动化/README.md）：
- 注册表不能直接装"已解压目录"；策略只认 CRX + HTTP 更新源
- 本机 HKCU\\SOFTWARE\\Policies 被组织策略锁为仅管理员可写（提权）
- 私钥 = 扩展身份（同 key 永远同 ID）：%USERPROFILE%\\.chrome_capture_operate_crx_key.pem
- 强装扩展 chrome://extensions 中开关置灰、无移除按钮
- chrome://policy 的"重新加载政策"按钮可让策略立即生效（否则等约
  3 分钟周期刷新或重启 Chrome）；页面内网页按钮的 UIA invoke() 有效
- wmic 在部分 Windows 版本（如新 Win11）已移除：进程枚举改用
  PowerShell Get-CimInstance（同等效果，需求「Windows wmic命令」）
- 输入法安全（需求「统一要求」）：拼音/五笔等中文输入法激活时逐键输入
  会被组合转换为汉字——URL 等文本一律经剪贴板粘贴输入（_type_text_
  ime_safe），快捷键（^t/^l/^v/{ENTER}）与粘贴均不经输入法组合
- UIA 一律按 HWND 定位（Application(uia).windows() 在 Chrome 上挂死）
- 对话框在 Chrome 独立宿主进程，全系统按 class #32770 + 标题搜；
  真对话框判据 = "文件名" Edit 可写入
"""
import ctypes
import json
import logging
import os
import subprocess
import threading
import time
import zipfile

log = logging.getLogger("app.ext_install")




def deps_check():
    """检查自动安装所需可选依赖在当前服务进程中是否可用。

    find_spec 只定位不执行，开销小；供状态接口返回，前端打开页面即提示，
    避免点击安装后才发现缺依赖。
    """
    import importlib.util
    return {"crx3": importlib.util.find_spec("crx3") is not None,
            "pywinauto": importlib.util.find_spec("pywinauto") is not None}


def sys_executable():
    import sys
    return sys.executable


# CRX 输出目录（chrome_capture_operate_python/crx，经本服务 /crx/ 接口
# 提供下载；.gitignore 排除 chrome_capture_operate_extension/crx，
# 静态产物放 python 项目目录便于随服务分发）
WORK_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "crx")
KEY_PATH = os.path.join(os.path.expanduser("~"),
                        ".chrome_capture_operate_crx_key.pem")
POLICY_KEY = r"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
UPDATE_PORT_DEFAULT = 33445   # 更新源由本服务自身提供（默认监听端口）

# 更新源被 Chrome 拉取的记录（webapp /crx/ 端点在 Chrome 请求
# update.xml / CRX 时调用 note_crx_fetch 记时间）。注册表安装后的
# "重新加载政策"步骤以此确认 Chrome 已发起更新检查（请求带
# installedby=policy），即扩展安装已实际触发
CRX_FETCH = {"update_xml": 0.0, "crx": 0.0}


# ---------- 解压版插件 ID 计算（需求：模拟点击安装成功后写入全局配置） ----------

def compute_unpacked_extension_id(ext_dir):
    """从扩展目录路径计算解压版插件的 Chrome 扩展 ID。

    Chrome 对无 key 字段的解压版扩展，ID = 加载目录绝对路径的
    SHA256 前 16 字节，每字节高低 nibble 各映射一个 a-p 字符。
    实测验证：路径不解码、直接 UTF-16LE 编码、不做小写化
    （本机已知 ID heojfigbamelbjpcigbldaiedgklbgjm 对照吻合）。

    注意：路径不变 → ID 不变；路径变（目录移动/换机）→ ID 变。
    """
    import hashlib
    path = os.path.abspath(ext_dir)
    data = path.encode("utf-16-le")
    h = hashlib.sha256(data).digest()[:16]
    out = []
    for b in h:
        out.append(chr(ord("a") + (b >> 4)))
        out.append(chr(ord("a") + (b & 0xF)))
    return "".join(out)


# ---------- 完成通知（需求「统一要求」：安装完毕让客户知道） ----------
# 页面进度日志之外的系统级信号：任务完成/失败时在桌面右下角弹出
# 30 秒后自动消失的窗口（main.py 注入 popup_notify.notify；未注入的
# 测试/无界面环境退化为日志），无论用户当时在看哪个窗口。

_notify_fn = None

KIND_LABELS = {"registry_install": "注册表安装",
               "ui_install": "模拟点击安装",
               "registry_uninstall": "注册表卸载"}


def set_notify(fn):
    """注册完成通知函数 fn(title, message)（main.py 注入托盘气泡）。"""
    global _notify_fn
    _notify_fn = fn


def notify_result(kind, ok, message=""):
    """安装/卸载任务完成通知（成功/失败均通知；托盘气泡或日志）。"""
    label = KIND_LABELS.get(kind, kind or "任务")
    if ok:
        text = "Chrome插件%s完成%s" % (
            label, ("：" + message[:100]) if message else "")
    else:
        text = "Chrome插件%s失败：%s" % (label, (message or "未知原因")[:100])
    fn = _notify_fn
    if fn:
        try:
            fn("chrome_capture_operate", text)
            return
        except Exception as e:
            log.warning("发送完成通知失败: %s", e)
    log.info("任务完成通知: chrome_capture_operate | %s", text)


def note_crx_fetch(kind):
    """记录更新源被访问（webapp /crx/ 端点调用，kind=update_xml|crx）。"""
    CRX_FETCH[kind] = time.time()


def _wait_crx_fetch(since, timeout=20):
    """等待 since 之后更新源被 Chrome 拉取（返回是否观察到）。

    政策重新加载后 Chrome 应立即向更新源发起更新检查；超时说明拉取
    未发生（或更早前已装同版本不再拉），由调用方按提示处理。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(CRX_FETCH[k] >= since for k in CRX_FETCH):
            return True
        time.sleep(0.5)
    return False


# ---------- 打包（CRX3 + RSA 私钥，改编自 install_via_registry.py） ----------

def pack_crx(ext_dir):
    """扩展目录 → CRX3。返回 (crx_path, ext_id, version)。

    私钥不存在则生成并永久复用（决定扩展 ID，勿丢失/更换）。
    ID 以 CRX 头内嵌 SignedData.crx_id 为准（ground truth）。
    """
    try:
        from crx3 import creator, crx3_pb2, id_util
    except ImportError:
        raise RuntimeError(
            "缺少 crx3 依赖（当前 Python: %s）——请用该 Python 执行 "
            "pip install crx3，或经 start.bat 用项目 .venv 启动服务" %
            sys_executable())
    import struct

    os.makedirs(WORK_DIR, exist_ok=True)
    manifest = json.load(open(os.path.join(ext_dir, "manifest.json"),
                              encoding="utf-8"))
    version = manifest["version"]
    zip_path = os.path.join(WORK_DIR, "chrome_capture_operate.zip")
    crx_path = os.path.join(WORK_DIR, "chrome_capture_operate.crx")

    if not os.path.exists(KEY_PATH):
        creator.create_private_key_file(KEY_PATH)
        log.info("已生成新私钥（扩展身份）: %s", KEY_PATH)

    out_root = os.path.abspath(os.path.join(ext_dir, "crx"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(ext_dir):
            if os.path.abspath(root) == out_root:
                continue    # 排除 crx 输出目录（防 CRX 嵌套自身）
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, ext_dir))

    creator.create_crx_file(zip_path, KEY_PATH, crx_path)

    data = open(crx_path, "rb").read()
    assert data[:4] == b"Cr24", "不是 CRX 文件"
    _ver, hdr_len = struct.unpack("<II", data[4:12])
    hdr = crx3_pb2.CrxFileHeader()
    hdr.ParseFromString(data[12:12 + hdr_len])
    sd = crx3_pb2.SignedData()
    sd.ParseFromString(hdr.signed_header_data)
    ext_id = id_util.convert_hex_crx_id_to_alphabet(sd.crx_id.hex())
    return crx_path, ext_id, version


def write_update_xml(ext_id, version, port):
    """本地更新源 update.xml（CRX 与 update.xml 由本服务 /crx/ 提供）。"""
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<gupdate xmlns="http://www.google.com/update2/response" '
           'protocol="2.0">\n'
           '  <app appid="%s">\n'
           '    <updatecheck '
           'codebase="http://127.0.0.1:%d/crx/chrome_capture_operate.crx" '
           'version="%s" />\n'
           '  </app>\n'
           '</gupdate>\n') % (ext_id, port, version)
    open(os.path.join(WORK_DIR, "update.xml"), "w",
         encoding="utf-8").write(xml)


# ---------- 策略（reg 文件 + regedit.exe，需求指定 UAC 仅限该步骤） ----------

def _policy_value():
    """读取当前策略值（无则 None）。"""
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, POLICY_KEY)
        got, _ = winreg.QueryValueEx(k, "1")
        winreg.CloseKey(k)
        return got
    except OSError:
        return None


def _run_regedit(reg_path):
    """提权运行 regedit 导入 reg 文件（UAC 弹出方 = regedit.exe）。"""
    import ctypes
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "regedit.exe", '/s "%s"' % reg_path, None, 1)
    if ret <= 32:
        raise RuntimeError("UAC 被取消——策略未生效")


def _write_reg_file(content, name):
    """写 .reg 文件（UTF-16LE + BOM，regedit 5.00 格式）。返回路径。"""
    os.makedirs(WORK_DIR, exist_ok=True)
    reg_path = os.path.join(WORK_DIR, name)
    open(reg_path, "w", encoding="utf-16").write(content)
    return reg_path


def write_policy(value, log_fn=None):
    """写入 Forcelist 策略：先试当前权限，锁死则 reg 文件 + regedit UAC。"""
    import winreg
    _log = log_fn or (lambda m: log.info(m))
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, POLICY_KEY, 0,
                               winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "1", 0, winreg.REG_SZ, value)
        winreg.CloseKey(k)
        _log("当前权限可直接写入策略（无需 UAC）")
        return
    except PermissionError:
        _log("当前权限不足（Policies 子树仅管理员可写），已生成 reg 文件，"
             "提权运行 regedit.exe 导入——请在弹出的 UAC 确认框点击\"是\"…")
    content = ("Windows Registry Editor Version 5.00\r\n\r\n"
               "[HKEY_CURRENT_USER\\%s]\r\n"
               '"1"="%s"\r\n') % (POLICY_KEY, value)
    reg_path = _write_reg_file(content, "install_policy.reg")
    _log("reg 文件已生成: %s" % reg_path)
    _run_regedit(reg_path)
    for i in range(30):
        time.sleep(0.5)
        if _policy_value() == value:
            return
        if (i + 1) % 10 == 0:
            _log("等待 UAC 确认与策略生效（已等 %d/15 秒）…" % ((i + 1) // 2))
    raise RuntimeError("策略写入未确认成功（UAC 未点击或超时），可重试")


def remove_policy():
    """删除 Forcelist 策略值（扩展保留，之后恢复可移除性）。"""
    import winreg
    try:
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, POLICY_KEY, 0,
                               winreg.KEY_SET_VALUE)
            winreg.DeleteValue(k, "1")
            winreg.CloseKey(k)
        except FileNotFoundError:
            pass
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, POLICY_KEY)
        except OSError:
            pass
        return
    except PermissionError:
        pass
    content = ("Windows Registry Editor Version 5.00\r\n\r\n"
               "[HKEY_CURRENT_USER\\%s]\r\n"
               '"1"=-\r\n') % POLICY_KEY
    reg_path = _write_reg_file(content, "remove_policy.reg")
    _run_regedit(reg_path)
    for _ in range(30):
        time.sleep(0.5)
        if _policy_value() is None:
            return
    raise RuntimeError("策略删除未确认成功，请人工检查")


# ---------- 输入法安全的文本输入（需求「统一要求」） ----------
# 操作系统输入法为拼音/五笔等中文状态时，逐键输入会被输入法组合转换
# 为汉字（chrome://extensions 等地址会被破坏）。解决：文本经剪贴板
# 粘贴（^v 快捷键与粘贴内容都不经输入法组合，与输入法状态无关）。

_CB_READY = False


def _cb_init():
    """声明剪贴板相关 Win32 API 的参数/返回类型。

    必须：Win64 上句柄/指针为 64 位，ctypes 未声明 restype 时默认按
    32 位 c_int 解释——GlobalAlloc 等返回值高位被截断后 GlobalLock
    失败（实测复现：中文文本偶发写入失败即此因）。声明一次进程内
    持久（windll 缓存函数对象）。
    """
    global _CB_READY
    if _CB_READY:
        return
    import ctypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.restype = ctypes.c_int
    u.CloseClipboard.argtypes = []
    u.CloseClipboard.restype = ctypes.c_int
    u.EmptyClipboard.argtypes = []
    u.EmptyClipboard.restype = ctypes.c_int
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.EnumClipboardFormats.argtypes = [ctypes.c_uint]
    u.EnumClipboardFormats.restype = ctypes.c_uint
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.restype = ctypes.c_int
    k.GlobalSize.argtypes = [ctypes.c_void_p]
    k.GlobalSize.restype = ctypes.c_size_t
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    k.GlobalFree.restype = ctypes.c_void_p
    _CB_READY = True


def _clipboard_set_text(text):
    """写剪贴板文本（CF_UNICODETEXT，经 ctypes，无新增依赖）。"""
    _cb_init()
    import ctypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    data = text.encode("utf-16-le") + b"\x00\x00"
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 2
    if not u.OpenClipboard(None):
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = k.GlobalLock(h)
        if not p:
            k.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        if not u.SetClipboardData(CF_UNICODETEXT, h):
            k.GlobalFree(h)
            return False
        return True
    finally:
        u.CloseClipboard()


def _clipboard_get_text():
    """读剪贴板文本；剪贴板无文本数据时返回 None。"""
    _cb_init()
    import ctypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    if not u.OpenClipboard(None):
        return None
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        size = k.GlobalSize(h)
        if not size:
            return None
        p = k.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.string_at(p, size).decode(
                "utf-16-le", "replace").split("\x00", 1)[0]
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _clipboard_has_data():
    """剪贴板是否有任意格式数据（用于区分"空剪贴板"与"非文本数据"）。"""
    _cb_init()
    u = ctypes.windll.user32
    if not u.OpenClipboard(None):
        return True        # 打不开（被占用）按"有数据"处理，保守不清空
    try:
        return bool(u.EnumClipboardFormats(0))   # 0 = 空剪贴板
    finally:
        u.CloseClipboard()


def _clipboard_empty():
    """清空剪贴板（还原"原剪贴板为空"的状态）。"""
    _cb_init()
    u = ctypes.windll.user32
    if u.OpenClipboard(None):
        try:
            u.EmptyClipboard()
        finally:
            u.CloseClipboard()


def _type_text_ime_safe(w32, text, log_fn=None):
    """向已聚焦的输入框以输入法安全方式输入文本（剪贴板粘贴）。

    粘贴前保存剪贴板文本、粘贴落定后恢复（留 0.3 秒给目标程序处理
    粘贴消息）：原为文本则恢复原文；原为空剪贴板则清空还原；原为
    非文本格式（图片/文件等）则无法恢复（注释见需求，尽力而为）。
    剪贴板写入失败（极少见）时退回逐键输入——pywinauto 默认经
    KEYEVENTF_UNICODE 直发字符（vk_packet=True），同样不经输入法组合。
    """
    _log = log_fn or (lambda m: None)
    saved = _clipboard_get_text()
    had_data = saved is not None or _clipboard_has_data()
    if not _clipboard_set_text(text):
        _log("剪贴板写入失败，退回 unicode 直发输入文本")
        w32.type_keys(text, pause=0.05, with_spaces=True)
        return
    try:
        w32.type_keys("^v", pause=0.05)
        time.sleep(0.3)   # 目标程序处理粘贴后再恢复剪贴板（避免竞态）
    finally:
        if saved is not None:
            _clipboard_set_text(saved)
        elif not had_data:
            _clipboard_empty()   # 原剪贴板为空：清空还原（非文本则无法恢复）


# ---------- 定位日常 Chrome 主进程（改编自 uninstall.py） ----------

def _chrome_processes():
    """枚举 chrome.exe 进程，返回 [(pid, 命令行), ...]。

    原用 wmic（需求「Windows wmic命令」：部分 Windows 版本如新 Win11
    已移除 wmic），改用同等效果的 PowerShell Get-CimInstance Win32_Process
    （Win10/11 均自带）+ UTF-8 输出 + JSON 解析。PowerShell 单结果时
    ConvertTo-Json 输出对象而非数组，调用方兼容两种形态。
    失败/超时/无进程返回 []。

    执行策略（"第一次不允许执行 PowerShell"）实测结论（2026-09-24，
    CurrentUser 策略设为最严格 Restricted 对照实验）：执行策略只拦
    .ps1 脚本文件，不拦 -Command 内联命令——本函数用 -Command，故
    无需提前执行 Set-ExecutionPolicy 等命令；-ExecutionPolicy Bypass
    为逐次豁免参数（不改系统全局状态），对 .ps1 场景也已足够。
    """
    ps = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
          "@(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
          "| Select-Object ProcessId, CommandLine) "
          "| ConvertTo-Json -Compress")
    try:
        out = (subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30).stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return []
    if not out:
        return []          # 无 chrome 进程（空管道无输出）
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]      # 单结果时为对象，归一为数组
    procs = []
    for it in data:
        if not isinstance(it, dict):
            continue
        pid = it.get("ProcessId")
        if isinstance(pid, int):
            procs.append((pid, it.get("CommandLine") or ""))
    return procs


def find_daily_pid():
    """日常 Chrome 主进程 PID（排除抓包/MCP/测试实例与 --type= 子进程）。

    主进程判据：命令行不含 --type=（Chrome 子进程均带）。
    """
    for pid, cl in _chrome_processes():
        if ("chrome_capture_operate_profile" in cl
                or "chrome-devtools-mcp" in cl
                or "_cco_crx_test" in cl):
            continue
        if "--type=" in cl:
            continue    # Chrome 子进程
        return pid
    return None


# ---------- 模拟点击安装（改编自 load_unpacked_extension.py） ----------

def _norm_ext_dir(ext_dir):
    """插件目录路径规范化：统一为 Windows 反斜杠形式并解析 ".."。

    问题记录（docs/问题记录/问题记录-插件自动安装路径分隔符与空格-
    20260930.md）：os.path.join 不规范化分隔符——基路径含正斜杠时
    结果是混合分隔符（"D:/a\\b"），Chrome 的"选择扩展程序目录"对话框
    对正斜杠/混合分隔符路径可能解析失败导致安装失败。写入对话框前
    统一规范化（Windows 下 abspath 把 / 转成 \\ 并解析 ./.. 段、
    转绝对路径）。含空格路径原样保留（对话框写入与后续环节均为
    原样字符串/加引号传参，空格安全）。
    """
    return os.path.normpath(os.path.abspath(ext_dir))


def _edit_value(edit):
    """读取对话框输入框当前文本（UIA ValuePattern 优先，失败退回 texts）。"""
    try:
        v = edit.get_value()
        if v is not None:
            return v
    except Exception:
        pass
    try:
        texts = edit.texts()
        return texts[0] if texts else ""
    except Exception:
        return ""


def _dialog_text_matches(written, expect):
    """对话框输入框内容与预期路径是否一致（normcase 大小写不敏感）。"""
    return os.path.normcase((written or "").strip()) == \
        os.path.normcase((expect or "").strip())


def install_ui(ext_dir, pid=None):
    """UI 自动化在已运行的日常 Chrome 中加载已解压扩展。

    全程约 10 秒：占用该 Chrome 键盘焦点（新开标签页导航，
    不动用户原有标签页）；期间人工不要操作鼠标与键盘。
    返回 (ok, message)。
    """
    try:
        from pywinauto import Application, Desktop
    except ImportError:
        return False, ("缺少 pywinauto 依赖（当前 Python: %s）——请用该 Python 执行 pip install pywinauto，或经 start.bat 用项目 .venv 启动服务" % sys_executable())

    pid = pid or find_daily_pid()
    if not pid:
        return False, "未找到日常 Chrome 主进程（可先打开日常 Chrome 再试）"
    # 路径规范化：统一反斜杠分隔符并解析 ..（正斜杠/混合分隔符路径
    # 可能使 Chrome 目录选择框解析失败，问题记录 2026-09-30）
    ext_dir = _norm_ext_dir(ext_dir)
    t0 = time.time()

    app32 = Application(backend="win32").connect(process=pid)
    w32 = app32.top_window()
    w32.set_focus()
    time.sleep(0.3)
    w32.type_keys("^t", pause=0.04)
    time.sleep(0.7)
    w32.type_keys("^l", pause=0.04)
    time.sleep(0.3)
    _type_text_ime_safe(w32, "chrome://extensions")
    w32.type_keys("{ENTER}", pause=0.02)
    time.sleep(1.2)

    def dialog_candidates():
        try:
            return [w for w in Desktop(backend="win32").windows(
                class_name="#32770")
                if "选择扩展程序目录" in w.window_text()]
        except Exception:
            return []

    uia_win = Desktop(backend="uia").window(handle=w32.handle)
    btn = uia_win.child_window(title="加载未打包的扩展程序",
                               control_type="Button")
    if not btn.exists(timeout=6):
        for tg in uia_win.descendants(title="开发者模式",
                                      control_type="Button"):
            try:
                tg.click_input()
            except Exception:
                continue
            time.sleep(1.0)
            if btn.exists(timeout=2):
                break
    if not btn.exists(timeout=6):
        return False, "找不到 '加载未打包的扩展程序' 按钮"
    btn.invoke()

    t_dlg = time.time()
    while time.time() - t_dlg < 3 and w32.is_enabled():
        time.sleep(0.03)
    deadline = time.time() + 3
    while time.time() < deadline and not dialog_candidates():
        time.sleep(0.1)

    uia_dlg = None
    written = False
    t_ed = time.time()
    deadline = time.time() + 10
    while time.time() < deadline:
        for w in dialog_candidates():
            uia = Desktop(backend="uia").window(handle=w.handle)
            try:
                edits = uia.descendants(control_type="Edit")
                cand = [e for e in edits
                        if "搜索" not in (e.element_info.name or "")]
                if cand:
                    try:
                        cand[0].set_edit_text(ext_dir)
                        # 回读校验：写入内容与预期路径必须一致——路径被
                        # 篡改（分隔符/空格/输入法）时本轮发现并重试，
                        # 而非等 Chrome 报"目录不存在"或静默装错目录
                        if _dialog_text_matches(_edit_value(cand[0]),
                                                ext_dir):
                            uia_dlg, written = uia, True
                            break
                    except Exception:
                        pass
            except Exception:
                pass
        if written:
            break
        time.sleep(0.12)
    if not written:
        return False, ("10 秒内没有找到可写入的路径输入框，或写入的路径"
                       "与预期不一致（%s）" % ext_dir)
    time.sleep(0.15)

    ok = uia_dlg.child_window(title="选择文件夹", control_type="Button")
    if not ok.exists(timeout=4, retry_interval=0.1):
        return False, "未找到 '选择文件夹' 按钮"
    ok.invoke()

    t_close = time.time()
    while time.time() - t_close < 6 and not w32.is_enabled():
        time.sleep(0.03)
    return True, "安装完成（耗时 %.1f 秒）" % (time.time() - t0)


# ---------- 程序操作Chrome重新加载政策（chrome://policy，需求指定） ----------

def reload_policy_ui(pid=None, log_fn=None, expect_present=True):
    """UI 自动化在已运行的日常 Chrome 打开 chrome://policy 并点"重新加载政策"。

    安装后使刚写入的 ExtensionInstallForcelist 策略立即生效（Chrome 随即
    从更新源拉取安装扩展）；卸载后使策略删除立即生效（扩展解除钉死）。
    替代人工点击/重启 Chrome/等约 3 分钟策略周期刷新。全程占用该 Chrome
    键盘焦点约 10 秒（新开标签页导航，不动用户原有标签页）；期间人工
    不要操作鼠标与键盘。
    expect_present：软校验预期——安装后 True（政策应出现在列表），
    卸载后 False（政策应从列表消失）。
    返回 (ok, message)。
    """
    try:
        from pywinauto import Application, Desktop
    except ImportError:
        return False, ("缺少 pywinauto 依赖（当前 Python: %s）——请用该 Python 执行 pip install pywinauto，或经 start.bat 用项目 .venv 启动服务" % sys_executable())

    _log = log_fn or (lambda m: log.info(m))
    pid = pid or find_daily_pid()
    if not pid:
        return False, ("未找到日常 Chrome 主进程（Chrome 未运行时无需重新加载，"
                       "下次启动会直接读取策略）")
    t0 = time.time()

    app32 = Application(backend="win32").connect(process=pid)
    w32 = app32.top_window()
    w32.set_focus()
    time.sleep(0.3)
    w32.type_keys("^t", pause=0.04)
    time.sleep(0.7)
    w32.type_keys("^l", pause=0.04)
    time.sleep(0.3)
    _type_text_ime_safe(w32, "chrome://policy", log_fn=_log)
    w32.type_keys("{ENTER}", pause=0.02)
    time.sleep(1.5)

    uia_win = Desktop(backend="uia").window(handle=w32.handle)
    btn = uia_win.child_window(title="重新加载政策", control_type="Button")
    if not btn.exists(timeout=6):
        return False, ("chrome://policy 页未找到'重新加载政策'按钮"
                       "（Chrome 界面语言非中文时需人工操作）")
    try:
        btn.invoke()
    except Exception:
        btn.click_input()
    _log("已点击'重新加载政策'，等待政策重新加载…")
    time.sleep(2.0)

    # 软校验：政策列表按预期变化（政策值列会被截断，按策略名整串匹配；
    # 未达成不影响结果，仅在消息中提示人工查看）
    found = False
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            has = any("ExtensionInstallForcelist" in
                      (e.element_info.name or "")
                      for e in uia_win.descendants(control_type="Text"))
        except Exception:
            has = False   # 查询未就绪：继续轮询
        if has == expect_present:
            found = True
            break
        time.sleep(0.5)
    # 用完关闭本页标签（导航新开的标签页；政策页标题含"政策"）
    try:
        if "政策" in w32.window_text():
            w32.set_focus()
            time.sleep(0.2)
            w32.type_keys("^w", pause=0.04)
    except Exception:
        pass
    msg = "已在 chrome://policy 点击'重新加载政策'（耗时 %.1f 秒）" % (time.time() - t0)
    if not found:
        msg += (("；未在政策列表确认到 ExtensionInstallForcelist"
                 if expect_present else
                 "；政策列表仍显示 ExtensionInstallForcelist（可能刷新未完成）")
                + "，可人工打开 chrome://policy 查看")
    return True, msg


# ---------- 安装完成后打开扩展程序页面（统一要求：结果可视化） ----------

def open_extensions_ui(pid=None, log_fn=None):
    """UI 自动化在日常 Chrome 新标签页打开 chrome://extensions 并保留。

    注册表安装成功后调用：页面停在扩展程序列表，插件卡片可见即安装
    完成的可视化确认。chrome.exe 直开 chrome:// 不可靠（docs/
    design-chrome-extensions-open.md 实测：单实例转交可能失败），故用
    与 install_ui 相同的键盘导航（已验证），约 3 秒；期间请勿操作键鼠。
    返回 (ok, message)。
    """
    try:
        from pywinauto import Application
    except ImportError:
        return False, ("缺少 pywinauto 依赖（当前 Python: %s）——请用该 Python 执行 pip install pywinauto，或经 start.bat 用项目 .venv 启动服务" % sys_executable())

    _log = log_fn or (lambda m: log.info(m))
    pid = pid or find_daily_pid()
    if not pid:
        return False, "未找到日常 Chrome 主进程"
    t0 = time.time()

    app32 = Application(backend="win32").connect(process=pid)
    w32 = app32.top_window()
    w32.set_focus()
    time.sleep(0.3)
    w32.type_keys("^t", pause=0.04)
    time.sleep(0.7)
    w32.type_keys("^l", pause=0.04)
    time.sleep(0.3)
    _type_text_ime_safe(w32, "chrome://extensions", log_fn=_log)
    w32.type_keys("{ENTER}", pause=0.02)
    time.sleep(1.0)
    return True, ("已打开 chrome://extensions（耗时 %.1f 秒，插件卡片可见）"
                  % (time.time() - t0))


# ---------- 注册表一键卸载（改编自 uninstall.py） ----------

def _unpin_after_policy_removal(pid, log_fn=None):
    """删策略后等待扩展解除钉死（需求：卸载时也自动重新加载政策）。

    自动在 chrome://policy 点"重新加载政策"使删除立即生效（约 10 秒，
    期间请勿操作键鼠）后短等页面状态稳定；自动重载失败（如 Chrome
    界面语言非中文、找不到按钮）时退回盲等约 3 分钟周期刷新，并提示
    可人工到 chrome://policy 点'重新加载政策'加快。
    """
    _log = log_fn or (lambda m: log.info(m))
    _log("程序将操作该 Chrome 打开 chrome://policy 并点击'重新加载政策'（约 10 秒，请勿操作鼠标与键盘）…")
    reload_ok, reload_msg = reload_policy_ui(pid=pid, log_fn=_log,
                                             expect_present=False)
    if reload_ok:
        _log("政策已重新加载（%s），短等扩展解除钉死…" % reload_msg)
        time.sleep(8)
        return
    _log("自动重新加载政策未成功（%s），退回等待 Chrome 周期性策略刷新"
         "（约 3 分钟）解除钉死；也可人工到 chrome://policy 点'重新加载"
         "政策'加快…" % reload_msg)
    blind = 200
    for i in range(blind // 20):
        time.sleep(20)
        _log("  已等 %d/%d 秒" % ((i + 1) * 20, blind))


def uninstall_registry(ext_id, pid=None, log_fn=None):
    """卸载注册表强装扩展：删策略（UAC）→ 自动重新加载政策 → UI 自动移除。

    删策略后程序自动操作 Chrome 在 chrome://policy 点"重新加载政策"，
    删除立即生效（扩展解除钉死，无需盲等约 3 分钟周期刷新）；自动重载
    失败（如 Chrome 界面语言非中文）时退回盲等。全程自动；
    返回 (ok, message)。
    """
    try:
        from pywinauto import Application, Desktop
    except ImportError:
        return False, ("缺少 pywinauto 依赖（当前 Python: %s）——请用该 Python 执行 pip install pywinauto，或经 start.bat 用项目 .venv 启动服务" % sys_executable())

    def _log(msg):
        (log_fn or (lambda m: log.info(m)))(msg)

    remove_policy()
    _log("策略已删除")

    pid = pid or find_daily_pid()
    if not pid:
        return False, "未找到日常 Chrome 主进程，策略已删除，请重启 Chrome 后到 chrome://extensions 手动移除"
    _log("目标 Chrome PID: %d" % pid)

    app32 = Application(backend="win32").connect(process=pid)
    w32 = app32.top_window()
    uia_win = Desktop(backend="uia").window(handle=w32.handle)

    def navigate():
        w32.set_focus()
        time.sleep(0.3)
        w32.type_keys("^t", pause=0.04)
        time.sleep(0.7)
        w32.type_keys("^l", pause=0.04)
        time.sleep(0.3)
        _type_text_ime_safe(w32, "chrome://extensions", log_fn=_log)
        w32.type_keys("{ENTER}", pause=0.02)
        time.sleep(2.5)

    def close_own_tab():
        try:
            if "扩展程序" in w32.window_text():
                w32.set_focus()
                time.sleep(0.2)
                w32.type_keys("^w", pause=0.04)
        except Exception:
            pass

    def id_text():
        try:
            for e in uia_win.descendants(control_type="Text"):
                if ext_id in (e.element_info.name or ""):
                    return e
        except Exception:
            pass
        return None

    def find_remove_button():
        anchor = id_text()
        if anchor is None:
            return None
        node = anchor
        for _ in range(10):
            try:
                node = node.parent()
            except Exception:
                return None
            try:
                for b in node.descendants(title="移除",
                                          control_type="Button"):
                    return b
            except Exception:
                continue
        return None

    # 删策略后解除扩展钉死：自动点"重新加载政策"立即生效，失败退回盲等
    # （本函数已运行在隔离子进程中，reload_policy_ui 直接进程内调用）
    _unpin_after_policy_removal(pid, log_fn=_log)

    _log("导航到 chrome://extensions（接下来几秒请勿操作键鼠）…")
    navigate()

    if id_text() is None:
        for tg in uia_win.descendants(title="开发者模式",
                                      control_type="Button"):
            try:
                tg.click_input()
            except Exception:
                continue
            time.sleep(1.0)
            if id_text() is not None:
                break

    if id_text() is None:
        close_own_tab()
        return True, "卡片不存在（Chrome 已随策略删除自动卸载，或已被移除）——完成"

    deadline = time.time() + 120
    rm = None
    while time.time() < deadline:
        rm = find_remove_button()
        if rm is not None:
            break
        if id_text() is None:
            close_own_tab()
            return True, "卡片已消失（自动卸载）——完成"
        time.sleep(5)
    if rm is None:
        return False, "2 分钟内未见目标卡片的'移除'按钮（策略已删除，可重启 Chrome 后手动移除）"

    try:
        w32.set_focus()
    except Exception:
        pass
    try:
        rm.invoke()
    except Exception:
        rm.click_input()

    t = time.time()
    done = False
    while time.time() - t < 30:
        if id_text() is None:
            done = True
            break
        try:
            for b in uia_win.descendants(title="移除", control_type="Button"):
                try:
                    b.invoke()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            for d in Desktop(backend="win32").windows(class_name="#32770"):
                uia_d = Desktop(backend="uia").window(handle=d.handle)
                for name in ("移除", "确定", "OK"):
                    try:
                        cand = uia_d.child_window(title=name,
                                                   control_type="Button")
                        if cand.exists(timeout=0.3):
                            cand.invoke()
                            break
                    except Exception:
                        continue
        except Exception:
            pass
        time.sleep(1)
    if done:
        close_own_tab()
        return True, "卸载完成（策略已删、扩展已移除）"
    return False, "30 秒内未确认卡片消失——请人工查看 chrome://extensions"


# ---------- 共享任务函数（Web 端点与 MCP 工具共用，均 HTTP 接口可达） ----------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def registry_ext_id():
    """注册表安装的扩展 ID（优先取策略值，兜底读 update.xml）。"""
    value = _policy_value()
    if value and ";" in value:
        return value.split(";")[0]
    xml_p = os.path.join(WORK_DIR, "update.xml")
    if os.path.isfile(xml_p):
        import re
        m = re.search(r'appid="([a-p]{32})"',
                      open(xml_p, encoding="utf-8").read())
        if m:
            return m.group(1)
    return None


def registry_install_run(ext_dir, port, log_fn=None):
    """注册表安装（同步，供 Web/MCP 调用）：打包 → 更新源 → 写策略 →
    自动重新加载政策。

    写策略需管理员：锁死时写 reg 文件并调 regedit.exe 导入
    （UAC 仅限该步骤，需人工点击确认，轮询确认最多 15 秒）。
    策略写入成功后自动操作日常 Chrome 在 chrome://policy 点"重新
    加载政策"（约 10 秒，期间请勿操作键鼠）使扩展立即安装；该步骤
    best-effort，失败时 Chrome 会在启动或约 3 分钟策略周期刷新时安装。
    """
    _log = log_fn or (lambda m: log.info(m))
    _log("打包 CRX（zip 压缩扩展目录 → CRX3 签名）…")
    crx_path, ext_id, version = pack_crx(ext_dir)
    _log("打包完成：CRX %d bytes，扩展 ID %s，版本 %s" % (
        os.path.getsize(crx_path), ext_id, version))
    _log("写入更新源文件 update.xml（更新源由本服务 /crx/ 提供）…")
    write_update_xml(ext_id, version, port)
    value = "%s;http://127.0.0.1:%d/crx/update.xml" % (ext_id, port)
    _log("写入注册表策略 ExtensionInstallForcelist（无权限时将弹出 UAC 确认框，请点击\"是\"；最长等待约 15 秒）…")
    write_policy(value, log_fn=_log)
    _log("策略写入成功")
    ok, msg = _policy_reload_step(log_fn=_log)
    if ok:
        # 安装已触发（统一要求：让客户知道安装完毕）——打开扩展程序
        # 页面展示插件卡片，配合托盘气泡通知构成完成信号
        _open_extensions_step(log_fn=_log)
        return True, ("注册表安装完成：%s；已打开 chrome://extensions 展示"
                      "安装结果；更新源由本服务提供，请保持服务运行" % msg)
    return True, ("注册表安装完成：%s；Chrome 将在启动或约 3 分钟策略周期"
                  "刷新时自动安装（也可人工到 chrome://policy 点'重新加载"
                  "政策'立即生效）；更新源由本服务提供，请保持服务运行" % msg)


def _subprocess_run(args, log_fn=None):
    """同步运行子进程命令（UI 自动化隔离运行，崩溃不波及服务）。

    逐行转发 stdout 到 log_fn；返回 (exit_code, 最后一行输出)。
    """
    import subprocess
    import sys
    _log = log_fn or (lambda m: None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.ext_install"] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=BASE_DIR,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    last = ""
    for line in proc.stdout:
        line = line.rstrip()
        if line.strip():
            _log(line)
            last = line
    code = proc.wait()
    return code, last


def _subprocess_fn(args, done_msg, fail_prefix):
    """构造子进程任务 fn（供 task.start 使用，转发子进程日志）。"""
    def fn(log_fn):
        code, _last = _subprocess_run(args, log_fn)
        return code == 0, (done_msg if code == 0
                           else fail_prefix + "（退出码 %d）" % code)
    return fn


def _policy_reload_step(log_fn=None):
    """"重新加载政策"步骤（注册表安装后自动执行，best-effort）。

    子进程 UI 自动化在 chrome://policy 点"重新加载政策"使策略立即
    生效；之后等待 Chrome 从更新源拉取（installedby=policy 的更新
    检查）以确认扩展安装已触发。返回 (ok, message)。
    """
    _log = log_fn or (lambda m: log.info(m))
    _log("程序将操作日常 Chrome 打开 chrome://policy 并点击'重新加载政策'"
         "（约 10 秒，请勿操作鼠标与键盘）…")
    t0 = time.time()
    code, last = _subprocess_run(["reload_policy"], _log)
    if code != 0:
        return False, (last or "政策重新加载子进程失败（退出码 %d）" % code)
    if _wait_crx_fetch(t0, timeout=20):
        _log("Chrome 已从更新源拉取（扩展自动安装已触发）")
        return True, "Chrome 已从更新源拉取，扩展自动安装已触发"
    return True, ("已点击'重新加载政策'（20 秒内未观察到 Chrome 拉取更新源，"
                  "可能稍后安装，可人工到 chrome://policy 查看）")


def _open_extensions_step(log_fn=None):
    """安装成功后打开 chrome://extensions 展示结果（best-effort，
    失败不影响安装）。UI 自动化经子进程运行（同 reload_policy）。"""
    _log = log_fn or (lambda m: log.info(m))
    _log("在日常 Chrome 打开 chrome://extensions 展示安装结果"
         "（新标签页保留，约 3 秒，请勿操作键鼠）…")
    code, last = _subprocess_run(["open_extensions"], _log)
    if code != 0:
        _log("打开 chrome://extensions 未成功（安装不受影响）：%s"
             % (last or "退出码 %d" % code))


def start_uninstall_task(ext_id):
    """启动注册表一键卸载后台任务（删策略→自动重载政策→UI 自动移除）。"""
    return task.start("registry_uninstall",
                      _subprocess_fn(["uninstall", ext_id or ""],
                                     "卸载完成", "卸载失败"),
                      timeout=600)


def start_ui_install_task(ext_dir):
    """启动模拟点击安装后台任务（UI 自动化，约 10 秒）。

    安装成功后自动计算解压版插件 ID 并写入全局配置（需求）——
    供"Chrome插件设置"页头按钮与插件设置页使用。
    """
    # 路径规范化（与 install_ui 同口径）：子进程命令行与插件 ID 计算
    # 都用规范化后的路径（问题记录 2026-09-30）
    ext_dir = _norm_ext_dir(ext_dir)

    def fn(log_fn):
        log_fn("程序将模拟人工点击操作日常 Chrome 完成安装（约 10 秒，请勿操作鼠标与键盘）…")
        inner = _subprocess_fn(["ui_install", ext_dir],
                               "安装完成", "安装失败")
        ok, msg = inner(log_fn)
        if ok:
            # 需求：安装成功后计算插件 ID 并写入全局配置
            ext_id = compute_unpacked_extension_id(ext_dir)
            if ext_id:
                from . import globalconf
                globalconf.set_value(globalconf.KEY_EXTENSION_ID, ext_id)
                log_fn("已计算插件 ID（%s）并写入全局配置" % ext_id)
        return ok, msg
    return task.start("ui_install", fn, timeout=90)

# ---------- 后台任务（供 webapp 调度） ----------

class ExtInstallTask:
    """安装/卸载后台任务：状态 + 日志（前端轮询展示）。

    - 异常时 done 置 False（前端显示 [失败] 而非停在无结果状态）
    - timeout 看门狗：UAC 无人确认（ShellExecuteW 阻塞）等场景下
      超时释放任务锁并报错，可重试；代次（_gen）保护使超时后旧
      线程的迟到写入不再生效
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.kind = None
        self.done = None
        self.error = None
        self.messages = []
        self._gen = 0

    def status(self):
        with self.lock:
            return {"running": self.running, "kind": self.kind,
                    "done": self.done, "error": self.error,
                    "messages": list(self.messages[-30:])}

    def start(self, kind, fn, timeout=None):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.kind = kind
            self.done = None
            self.error = None
            self.messages = []
            self._gen += 1
            gen = self._gen

        # 统一要求：执行期间桌面周边显示绿色边框（腾讯会议录屏同款，
        # 操作完毕/超时结束；点击穿透不影响键鼠）
        from . import screen_border
        screen_border.show()

        if timeout:
            threading.Timer(timeout, self._timeout_fire,
                            args=(gen, timeout)).start()

        def _log(msg):
            with self.lock:
                self.messages.append("[%s] %s" % (
                    time.strftime("%H:%M:%S"), msg))

        def _run():
            notify = None   # (ok, message)：仅当前代次（未被超时接管）才通知
            try:
                ok, msg = fn(_log)
                with self.lock:
                    if gen == self._gen:
                        self.done = bool(ok)
                        if msg:
                            self.messages.append("[%s] %s" % (
                                time.strftime("%H:%M:%S"), msg))
                        notify = (bool(ok), msg or "")
            except Exception as e:
                with self.lock:
                    if gen == self._gen:
                        self.done = False
                        self.error = str(e)
                        notify = (False, str(e))
            finally:
                with self.lock:
                    if gen == self._gen:
                        self.running = False
                # 统一要求：操作完毕结束绿色边框（超时路径在
                # _timeout_fire 结束；hide 幂等）
                from . import screen_border
                screen_border.hide()
                # 统一要求：任务完成/失败时通知用户（右下角弹窗，见
                # notify_result；超时已由 _timeout_fire 通知，迟到结果跳过）
                if notify:
                    notify_result(self.kind, notify[0], notify[1])

        threading.Thread(target=_run, daemon=True).start()
        return True

    def _timeout_fire(self, gen, seconds):
        with self.lock:
            if not self.running or gen != self._gen:
                return
            self.running = False
            self.done = False
            self._gen += 1    # 旧线程迟到写入失效
            self.error = ("任务超时（%d 秒未完成，可能 UAC 未确认或流程"
                          "卡住），任务锁已释放，可重试" % seconds)
            self.messages.append("[%s] %s" % (
                time.strftime("%H:%M:%S"), self.error))
        # 统一要求：超时（看门狗接管）也结束绿色边框并通知
        from . import screen_border
        screen_border.hide()
        notify_result(self.kind, False, self.error)   # 统一要求：超时也通知


task = ExtInstallTask()


# ---------- 子进程入口（UI 自动化必须独立进程：原生崩溃不波及服务） ----------
# 服务进程经 subprocess 调用：python -m app.ext_install ui_install <ext_dir>
#                          python -m app.ext_install reload_policy
#                          python -m app.ext_install open_extensions
#                          python -m app.ext_install uninstall <ext_id>
# 输出逐行打到 stdout（服务侧读取后作为任务日志展示），退出码 0=成功。


def _default_ext_dir():
    return _norm_ext_dir(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "..",
        "chrome_capture_operate_extension"))


def main():
    import sys
    args = sys.argv[1:]
    if not args:
        print("用法: python -m app.ext_install ui_install|reload_policy|open_extensions|uninstall [...]")
        return
    threading.Thread(   # 看门狗：防 UI 自动化无声挂死（同 docs 脚本）
        target=lambda: (time.sleep(720), os._exit(9)),
        daemon=True).start()
    if args[0] == "ui_install":
        ok, msg = install_ui(args[1] if len(args) > 1
                             else _default_ext_dir())
        print(msg, flush=True)
        sys.exit(0 if ok else 1)
    elif args[0] == "reload_policy":
        ok, msg = reload_policy_ui(
            log_fn=lambda m: print(m, flush=True))
        print(msg, flush=True)
        sys.exit(0 if ok else 1)
    elif args[0] == "open_extensions":
        ok, msg = open_extensions_ui(
            log_fn=lambda m: print(m, flush=True))
        print(msg, flush=True)
        sys.exit(0 if ok else 1)
    elif args[0] == "uninstall":
        ext_id = args[1] if len(args) > 1 else ""
        ok, msg = uninstall_registry(
            ext_id or None, log_fn=lambda m: print(m, flush=True))
        print(msg, flush=True)
        sys.exit(0 if ok else 1)
    else:
        print("未知命令: %s" % args[0])
        sys.exit(2)
if __name__ == "__main__":
    main()
