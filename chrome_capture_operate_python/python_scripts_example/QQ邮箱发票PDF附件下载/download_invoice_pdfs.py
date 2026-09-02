# -*- coding: utf-8 -*-
"""
QQ 邮箱发票 PDF 附件批量下载脚本（搜索版）

处理流程（与人工网页操作一致，基于抓包还原）：
1. 调用 /list/search 搜索邮件：keyword=发票、dirid=1（收件箱）、
   page_size=50，从 page_now=0 开始按页获取（搜索范围含标题/正文/附件，
   与网页“关键字位置=不限”一致）；
2. 每页内按时间从新到旧处理（搜索结果本身按时间降序返回）：
   - 标题含「发票/报销」且有 PDF 附件：
     - 未读：readmail 打开（置已读，与人工一致）后按规则下载——
       单个 PDF 直接下载；多个 PDF 仅下载文件名含「发票/报销」的，
       含「行程」的跳过；
     - 已读：说明更早的邮件已处理过，记录后立即结束；
   - 标题含「发票/报销」但无 PDF 附件（如正文仅链接的发票通知）：
     readmail 后记入汇总第三类（发现其他形式的发票邮件）；
   - 标题不含关键字但有 PDF 附件：记入汇总第二类（不打开邮件）；
3. 若当前页未遇到“满足结束条件的已读邮件”，则继续取下一页；
4. PDF 下载到桌面下以当前时间（毫秒级）命名的子目录，同名文件自动加序号；
5. 下载目录生成《发票附件下载汇总.html》：三类明细，其中第二、三类
   （发现其他情况的发票邮件）与下载失败等需人工关注的信息重点显示。

会话 sid 说明（实际验证结论）：
  wx.mail.qq.com 的会话 sid 必须取 Cookie 的 **xm_sid**；.mail.qq.com 域上
  的 sid 是 QQ 域登录 sid（形如 "xxx&"），拿它请求 xmlistlogicsvr 接口
  会返回 ret=-20002（stack=登录态失效）。脚本优先取 xm_sid，并以 folderlist
  响应的 param.sid 校准。

依赖 Cookie 服务 chrome_capture_operate（默认 http://127.0.0.1:33445）。
"""

import html
import os
import random
import re
import sys
from datetime import datetime

import requests
import urllib3

# 按项目约定不校验 HTTPS 证书（见 README 注意事项），
# 屏蔽随之产生的 InsecureRequestWarning 噪音（仅影响本进程）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Windows 控制台避免 GBK 编码问题
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "https://wx.mail.qq.com"
COOKIE_API = "http://127.0.0.1:33445/api/cookies/query"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 用户指定的搜索条件
SEARCH_KEYWORD = "发票"   # 搜索关键字（标题/正文/附件不限）
SEARCH_DIRID = 1          # 所在文件夹：收件箱
PAGE_SIZE = 50            # 每页数量（与抓包一致）
MAX_SEARCH_PAGES = 20     # 翻页上限（抓包 total_num=885，约 18 页）

SUBJECT_KEYWORDS = ("发票", "报销")   # 邮件标题关键字
ATTACH_KEYWORDS = ("发票", "报销")    # 多个 PDF 时的文件名关键字
ATTACH_SKIP_KEYWORD = "行程"          # 多个 PDF 时文件名含此关键字则跳过
SUMMARY_FILENAME = "发票附件下载汇总.html"


def get_desktop_dir():
    """获取桌面目录（兼容 OneDrive 等重定向：优先注册表 User Shell Folders）"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        value, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        return os.path.expandvars(value)
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Desktop")


# 下载目录：桌面下以当前时间（到毫秒级）命名的子目录，如 20260831_095500_123
DOWNLOAD_DIR = os.path.join(
    get_desktop_dir(),
    datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3])


def gen_r():
    """接口 r 参数：25~26 位随机数字（防缓存，与抓包观察一致）"""
    return "".join(random.choice("0123456789") for _ in range(26))


def get_cookie_header():
    """从 chrome_capture_operate 的 Cookie 服务获取 wx.mail.qq.com 的 Cookie"""
    try:
        r = requests.get(COOKIE_API, params={"url": BASE + "/"},
                         timeout=10, verify=False)
    except requests.RequestException as e:
        print("[错误] Cookie 服务不可达（chrome_capture_operate 未启动？）:", e)
        sys.exit(1)
    if r.status_code != 200:
        try:
            print("[错误] 获取 Cookie 失败:", r.json().get("error"))
        except Exception:
            print("[错误] 获取 Cookie 失败，状态码:", r.status_code)
        sys.exit(1)
    return r.json()["cookie_header"]


def build_session():
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": BASE,
        "Referer": BASE + "/",
    })
    s.headers["Cookie"] = get_cookie_header()
    return s


def parse_cookies(cookie_header):
    """Cookie 头字符串 -> {name: value}"""
    jar = {}
    for part in (cookie_header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def extract_sid_from_cookie(cookie_header):
    """从 Cookie 中取会话 sid。

    注意：wx.mail.qq.com 的会话 sid 是 **xm_sid**；.mail.qq.com 上的 sid
    是 QQ 域登录 sid（形如 "xxx&"），拿它请求 xmlistlogicsvr 接口会
    返回 ret=-20002（stack=登录态失效）。取值时去掉尾部 "&" 等拼接残留。
    """
    jar = parse_cookies(cookie_header)
    for name in ("xm_sid", "sid"):
        val = (jar.get(name) or "").split("&")[0].strip()
        if val:
            return val
    return ""


def post_form(session, path, params, sid):
    """POST form 请求，公共参数 language/r/sid，返回 JSON（ret==0）"""
    data = {"language": "zh", "r": gen_r(), "sid": sid}
    data.update(params)
    resp = session.post(BASE + path, data=data, timeout=60)
    resp.raise_for_status()
    j = resp.json()
    head = j.get("head") or {}
    if head.get("ret") != 0:
        # 服务端真正的原因常在 stack 里（如 ret=-20002 stack=登录态失效），
        # 而 msg 往往为空，故一并带上便于排查
        detail = head.get("msg") or head.get("stack") or ""
        if head.get("ret") == -20002:
            detail = ("%s（登录态失效：请确认浏览器已登录 wx.mail.qq.com，"
                      "且插件已推送其 Cookie）" % (detail or "-")).strip()
        raise RuntimeError("接口 %s 返回 ret=%s, msg=%s" %
                           (path, head.get("ret"), detail))
    return j


def fetch_sid(session, sid):
    """通过 folderlist 校准 sid（响应 param.sid 为当前会话 sid）。

    sid 与浏览器会话绑定、会轮换：Cookie 服务里存的是插件推送的**快照**，
    可能滞后于浏览器中的最新值。首次请求若报 ret=-20002（登录态失效），
    先重新取一次最新 Cookie 并用新 sid 重试一次；仍失败则说明浏览器端
    会话已被服务端作废，给出"需重新登录"的明确指引。
    """
    try:
        j = post_form(session, "/list/folderlist", {}, sid)
    except RuntimeError as e:
        if "ret=-20002" not in str(e):
            raise
        new_header = get_cookie_header()
        new_sid = extract_sid_from_cookie(new_header)
        if not new_sid or new_sid == sid:
            raise RuntimeError(
                "登录态失效（ret=-20002）：Cookie 服务中的 xm_sid 已被服务端作废，"
                "且浏览器尚未产生新的会话 sid。"
                "\n请在浏览器打开 https://wx.mail.qq.com 确认仍处于登录状态"
                "（已掉线则重新登录），插件会在 Cookie 变更后 10 秒内自动推送；"
                "也可在插件『推送记录』页点『立即推送』后重新运行本脚本。")
        print("    Cookie 快照已刷新（新 sid），重试 folderlist ...")
        session.headers["Cookie"] = new_header
        j = post_form(session, "/list/folderlist", {}, new_sid)
    return (j.get("param") or {}).get("sid") or sid


def search_page(session, sid, page):
    """搜索一页邮件（keyword + 收件箱），返回 (邮件列表, total_num)
    抓包依据：POST /list/search  page_now=<页号从0>&page_size=50&keyword=发票&dirid=1
    响应 body.list[] 含 unread/normal_attach，body.total_num 为总数"""
    params = {
        "page_now": page,
        "page_size": PAGE_SIZE,
        "keyword": SEARCH_KEYWORD,
        "dirid": SEARCH_DIRID,
    }
    j = post_form(session, "/list/search", params, sid)
    body = j.get("body") or {}
    return body.get("list") or [], int(body.get("total_num") or 0)


def is_pdf_attach(a):
    name = (a.get("name") or "").lower()
    return a.get("type") == "pdf" or name.endswith(".pdf")


def read_mail(session, sid, mailid):
    """读取邮件详情，返回 (info, normal_attach, subject, content)

    已读状态机制（抓包分析结论，详见 README）：
    - readmail func=1 打开邮件后服务端立即置为已读（收件箱 unread_num 在
      每次 readmail 后递减 1，无需其他专门请求；重新搜索亦确认已读）；
    - 抓包中唯一出现的专门请求 mgr/mailmgr func=4 仅与 readmail func=6
      （预览式打开）组合出现 1 次，同样达到已读效果，脚本无需模拟；
    - 前一晚已读邮件次日变回未读：经人工说明为验证而手动翻转状态，
      非系统回滚，不影响停止机制的可靠性。
    """
    j = post_form(session, "/read/readmail", {"mailid": mailid, "func": 1}, sid)
    item = (j.get("body") or {}).get("item") or {}
    info = item.get("info") or {}
    return (info, item.get("normal_attach") or [], item.get("subject") or "",
            item.get("content") or "")


def extract_invoice_links(content):
    """提取邮件正文 HTML 中所有非 QQ 邮箱域名的链接（<a href>）。

    用于第三类（标题含关键字但无 PDF 附件）邮件：正文常仅提供
    发票下载链接（如 fpkj.vpiaotong.com、fp.bwjf.cn 等），提取后
    供人工打开下载发票。排除 qq.com 及其子域（QQ 邮箱自身链接）、
    mailto:/javascript: 等非网页链接。
    """
    links, seen = [], set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', content or "", re.I):
        url = m.group(1).strip()
        if not url or url.startswith(("mailto:", "javascript:")):
            continue
        dm = re.match(r'https?://([^/]+)', url, re.I)
        if not dm:
            continue
        host = dm.group(1).lower()
        if host == "qq.com" or host.endswith(".qq.com"):
            continue
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "attachment.pdf"


def fmt_time(ts):
    """格式化邮件接收时间（totime，精确到秒）"""
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def fmt_sender(m):
    try:
        s = (m.get("senders") or {}).get("item") or [{}]
        return "%s <%s>" % (s[0].get("nick", ""), s[0].get("email", ""))
    except Exception:
        return "-"


def download_attachment(session, attach, sid):
    """下载单个附件（优先 readmail 返回的带 sid download_url；
    否则使用搜索结果的 download_url 并补充 sid 参数）"""
    url = attach.get("download_url") or ""
    if not url:
        raise RuntimeError("附件无 download_url")
    if "sid=" not in url:
        url += ("&" if "?" in url else "?") + "sid=" + sid
    name = sanitize_filename(attach.get("name") or "attachment.pdf")
    resp = session.get(BASE + url, timeout=300)
    resp.raise_for_status()
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    path = os.path.join(DOWNLOAD_DIR, name)
    # 重名文件加序号
    if os.path.exists(path):
        base, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(path):
            path = os.path.join(DOWNLOAD_DIR, "%s(%d)%s" % (base, i, ext))
            i += 1
    with open(path, "wb") as f:
        f.write(resp.content)
    return path, len(resp.content)


def process_page(session, sid, mails, seen, state):
    """处理一页搜索结果（按时间降序），更新 state
    state: downloaded / list_a / list_b / list_c / stopped_subject"""
    for idx, m in enumerate(mails, 1):
        emailid = m.get("emailid") or ""
        if not emailid or emailid in seen:
            continue
        seen.add(emailid)

        subject = m.get("subject") or ""
        sender = fmt_sender(m)
        date_str = fmt_time(m.get("totime", 0))
        has_kw = any(k in subject for k in SUBJECT_KEYWORDS)

        if not has_kw:
            # 第二类：标题不含关键字但有 PDF 附件（不打开邮件，避免置已读）
            pdfs_meta = [a for a in (m.get("normal_attach") or [])
                         if is_pdf_attach(a)]
            if pdfs_meta:
                state["list_b"].append({
                    "subject": subject, "sender": sender, "date": date_str,
                    "pdfs": [{"name": a.get("name") or "", "size": a.get("size", 0)}
                             for a in pdfs_meta],
                })
            continue

        # 标题含关键字：搜索结果自带 unread 与附件列表，先据此判断
        unread = int(m.get("unread", 0) or 0)
        pdfs_meta = [a for a in (m.get("normal_attach") or []) if is_pdf_attach(a)]

        print("\n--- %s" % subject)
        print("    %s  %s" % (sender, date_str))

        if not unread and pdfs_meta:
            # 已读且满足条件 => 停止点（不再打开邮件）
            print("    该邮件为【已读】且满足标题/PDF条件 => 更早的邮件已处理过，结束")
            entry = {
                "subject": subject, "sender": sender, "date": date_str,
                "unread": False, "stopped": True,
                "pdfs": [{"name": a.get("name") or "", "size": a.get("size", 0),
                          "status": "未下载（已读停止点，此前已处理）", "saved": ""}
                         for a in pdfs_meta],
            }
            state["list_a"].append(entry)
            state["stopped_subject"] = subject
            return True

        # 未读（或已读但无 PDF 附件）：打开邮件获取权威数据（副作用：置已读，与人工一致）
        try:
            info, attaches, subject2, content = read_mail(session, sid, emailid)
        except Exception as e:
            print("    [警告] 读取邮件失败：%s，跳过" % e)
            continue
        if subject2:
            subject = subject2
        unread = int(info.get("unread", 0) or 0)
        pdfs = [a for a in attaches if is_pdf_attach(a)]

        if not pdfs:
            # 第三类：标题含关键字但无 PDF 附件
            others = [a.get("name") for a in attaches if a.get("name")]
            links = extract_invoice_links(content)
            print("    标题含关键字但无 PDF 附件（%s），记录不下载，正文非QQ链接 %d 个"
                  % ("其他附件: " + ", ".join(others) if others else "无附件", len(links)))
            state["list_c"].append({
                "subject": subject, "sender": sender, "date": date_str,
                "unread": bool(unread), "others": others, "links": links,
            })
            continue

        if not unread:
            # readmail 后发现已读且满足条件 => 停止点
            print("    该邮件为【已读】且满足标题/PDF条件 => 更早的邮件已处理过，结束")
            entry = {
                "subject": subject, "sender": sender, "date": date_str,
                "unread": False, "stopped": True,
                "pdfs": [{"name": a.get("name") or "", "size": a.get("size", 0),
                          "status": "未下载（已读停止点，此前已处理）", "saved": ""}
                         for a in pdfs],
            }
            state["list_a"].append(entry)
            state["stopped_subject"] = subject
            return True

        # 未读且满足条件：下载
        entry = {
            "subject": subject, "sender": sender, "date": date_str,
            "unread": True, "stopped": False, "pdfs": [],
        }
        single = (len(pdfs) == 1)
        print("    未读，共 %d 个 PDF 附件%s"
              % (len(pdfs), "（单个，直接下载）" if single else "，按文件名筛选"))
        for a in pdfs:
            name = a.get("name") or ""
            rec = {"name": name, "size": a.get("size", 0),
                   "status": "", "saved": ""}
            skip_reason = None
            if not single:
                if ATTACH_SKIP_KEYWORD in name:
                    skip_reason = "跳过（文件名含“%s”）" % ATTACH_SKIP_KEYWORD
                elif not any(k in name for k in ATTACH_KEYWORDS):
                    skip_reason = "跳过（多个PDF，文件名不含 %s）" % "/".join(ATTACH_KEYWORDS)
            if skip_reason:
                print("    %s: %s" % (name, skip_reason))
                rec["status"] = skip_reason
            else:
                try:
                    path, size = download_attachment(session, a, sid)
                    state["downloaded"].append(path)
                    rec["status"] = "已下载" + ("（单个PDF直接下载）" if single else "")
                    rec["saved"] = os.path.basename(path)
                    print("    已下载: %s (%d 字节)" % (rec["saved"], size))
                except Exception as e:
                    rec["status"] = "下载失败：%s" % e
                    print("    [警告] 下载失败 %s: %s" % (name, e))
            entry["pdfs"].append(rec)
        state["list_a"].append(entry)

    return False


def _e(text):
    return html.escape(str(text or ""), quote=True)


def write_summary(state, total_num, pages_done):
    """在下载目录生成汇总文件（HTML）

    重点显示（需人工关注）：
    - 第二类：有 PDF 附件但标题不含关键字（可能漏掉的发票邮件）
    - 第三类：标题含关键字但无 PDF 附件（其他形式的发票邮件，如正文仅链接）
    - 下载失败的附件、已读停止点
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    path = os.path.join(DOWNLOAD_DIR, SUMMARY_FILENAME)
    list_a, list_b, list_c = state["list_a"], state["list_b"], state["list_c"]
    css = """
      body { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; margin: 24px;
             color: #222; background: #fafafa; }
      h1 { font-size: 22px; border-bottom: 2px solid #4a76a8; padding-bottom: 8px; }
      h2 { font-size: 18px; margin-top: 32px; }
      .warn h2 { color: #c0392b; }
      .meta { background: #eef3f8; border: 1px solid #d4e0ec; border-radius: 6px;
              padding: 10px 14px; line-height: 1.8; }
      .meta b { color: #2c5f8a; }
      .attention { background: #fdecea; border: 2px solid #e74c3c; border-radius: 6px;
                   padding: 10px 14px; margin: 12px 0; }
      .attention h2 { color: #c0392b; margin: 4px 0 8px 0; }
      .attention table { background: #fff; }
      table { border-collapse: collapse; width: 100%; margin: 10px 0;
              background: #fff; font-size: 13px; }
      th { background: #4a76a8; color: #fff; padding: 6px 10px; text-align: left; }
      td { border: 1px solid #d0d7de; padding: 6px 10px; vertical-align: top; }
      tr:nth-child(even) td { background: #f5f7fa; }
      .ok { color: #1e8e3e; font-weight: bold; }
      .fail { color: #d93025; font-weight: bold; background: #fdecea; }
      .skip { color: #7a7a7a; }
      .stop { color: #b26a00; font-weight: bold; background: #fef3e0; }
      .empty { color: #888; margin: 8px 0; }
      code { background: #eef1f4; border-radius: 3px; padding: 1px 5px;
             font-size: 12px; word-break: break-all; }
    """
    p = []
    p.append("<!DOCTYPE html>")
    p.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    p.append("<title>QQ邮箱发票PDF附件下载汇总</title>")
    p.append("<style>%s</style></head><body>" % css)
    p.append("<h1>QQ邮箱发票PDF附件下载汇总</h1>")

    # 运行信息
    p.append('<div class="meta">')
    p.append("运行时间：<b>%s</b><br>" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    p.append("下载目录：<b>%s</b><br>" % _e(DOWNLOAD_DIR))
    p.append("搜索条件：关键字「<b>%s</b>」（标题/正文/附件），所在文件夹=收件箱（dirid=%s）<br>"
             % (_e(SEARCH_KEYWORD), SEARCH_DIRID))
    p.append("搜索结果总数：<b>%d</b>，已处理 %d 页（每页 %d 封，按时间从新到旧）<br>"
             % (total_num, pages_done, PAGE_SIZE))
    if state["stopped_subject"]:
        p.append('停止情况：<span class="stop">遇到已读的满足条件邮件「%s」，更早的邮件未处理</span>'
                 % _e(state["stopped_subject"]))
    else:
        p.append("停止情况：未遇到满足条件的已读邮件，已处理完搜索范围内邮件（或达到翻页上限 %d 页）"
                 % MAX_SEARCH_PAGES)
    p.append("</div>")

    # 第一类：处理与下载明细
    p.append("<h2>一、标题含“发票/报销”且有PDF附件的邮件（处理与下载明细）</h2>")
    if not list_a:
        p.append('<p class="empty">（无）</p>')
    else:
        p.append('<table><tr><th>#</th><th>邮件标题</th><th>发件人</th>'
                 '<th>接收时间</th><th>未读</th><th>备注</th><th>PDF附件</th></tr>')
        for i, e in enumerate(list_a, 1):
            note = ('<span class="stop">已读停止点，未下载</span>'
                    if e["stopped"] else "已处理")
            # 附件明细单元格：逐个附件状态
            cells = []
            for a in e["pdfs"]:
                if a["status"].startswith("已下载"):
                    st = '<span class="ok">%s</span>' % _e(a["status"])
                elif a["status"].startswith("下载失败"):
                    st = '<span class="fail">%s</span>' % _e(a["status"])
                elif a["status"].startswith("未下载"):
                    st = '<span class="stop">%s</span>' % _e(a["status"])
                else:
                    st = '<span class="skip">%s</span>' % _e(a["status"])
                saved = (" → <code>%s</code>" % _e(a["saved"])) if a["saved"] else ""
                cells.append("<code>%s</code>（%s 字节）：%s%s"
                             % (_e(a["name"]), a["size"], st, saved))
            p.append("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td>%s</td><td>%s</td></tr>" % (
                         i, _e(e["subject"]), _e(e["sender"]), _e(e["date"]),
                         "是" if e["unread"] else "否", note,
                         "<br>".join(cells) if cells else "（无PDF）"))
        p.append("</table>")

    # 第二类：重点显示
    p.append('<div class="attention">')
    p.append("<h2>⚠ 二、有PDF附件但标题不含“发票/报销”的邮件"
             "（重点检查：可能是漏掉的发票邮件）</h2>")
    if not list_b:
        p.append('<p class="empty">（无）</p>')
    else:
        p.append('<table><tr><th>#</th><th>邮件标题</th><th>发件人</th>'
                 '<th>接收时间</th><th>PDF附件</th></tr>')
        for i, e in enumerate(list_b, 1):
            pdfs = "<br>".join("<code>%s</code>（%s 字节）" % (_e(a["name"]), a["size"])
                               for a in e["pdfs"])
            p.append("<tr><td>%d</td><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (i, _e(e["subject"]), _e(e["sender"]), _e(e["date"]), pdfs))
        p.append("</table>")
    p.append("</div>")

    # 第三类：重点显示
    p.append('<div class="attention">')
    p.append("<h2>⚠ 三、标题含“发票/报销”但无PDF附件的邮件"
             "（重点检查：其他形式的发票邮件；正文链接可点击打开后下载发票）</h2>")
    if not list_c:
        p.append('<p class="empty">（无）</p>')
    else:
        p.append('<table><tr><th>#</th><th>邮件标题</th><th>发件人</th>'
                 '<th>接收时间</th><th>未读</th><th>附件情况</th>'
                 '<th>正文非QQ邮箱链接（供人工打开下载发票）</th></tr>')
        for i, e in enumerate(list_c, 1):
            others = ("<br>".join("<code>%s</code>" % _e(n) for n in e["others"])
                      if e["others"] else "无附件")
            links = e.get("links") or []
            if links:
                link_html = "<br>".join(
                    '<a href="%s" target="_blank" rel="noopener">%s</a>'
                    % (_e(u), _e(u)) for u in links)
            else:
                link_html = '<span class="empty">（无）</span>'
            p.append("<tr><td>%d</td><td><b>%s</b></td><td>%s</td><td>%s</td>"
                     "<td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (i, _e(e["subject"]), _e(e["sender"]), _e(e["date"]),
                        "是" if e["unread"] else "否", others, link_html))
        p.append("</table>")
    p.append("</div>")

    p.append("</body></html>")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(p))
    return path


def main():
    print("=" * 70)
    print("QQ 邮箱发票 PDF 附件批量下载（搜索版）")
    print("搜索条件：关键字=「%s」（位置不限），所在文件夹=收件箱" % SEARCH_KEYWORD)
    print("=" * 70)

    session = build_session()
    sid = extract_sid_from_cookie(session.headers["Cookie"])

    print("\n[0/3] 校准 sid（取 Cookie 的 xm_sid，并以 folderlist 响应校准）...")
    try:
        sid = fetch_sid(session, sid)
    except RuntimeError as e:
        # 登录态失效等前置错误：打印明确原因后退出，不抛 traceback
        print("\n[错误] %s" % e)
        sys.exit(1)
    print("sid 获取成功")

    state = {
        "downloaded": [], "list_a": [], "list_b": [], "list_c": [],
        "stopped_subject": None,
    }
    seen = set()
    total_num = 0
    pages_done = 0

    print("\n[1/3] 按页搜索并处理邮件 ...")
    for page in range(MAX_SEARCH_PAGES):
        print("\n== 搜索第 %d 页（page_now=%d）==" % (page + 1, page))
        try:
            mails, total_num = search_page(session, sid, page)
        except Exception as e:
            print("  [警告] 搜索失败：%s，停止翻页" % e)
            break
        print("  本页返回 %d 封（总数 %d）" % (len(mails), total_num))
        pages_done = page + 1
        if not mails:
            break
        stopped = process_page(session, sid, mails, seen, state)
        if stopped:
            break
        if (page + 1) * PAGE_SIZE >= total_num:
            print("  已取完搜索结果")
            break

    print("\n[2/3] 生成汇总文件 ...")
    summary_path = write_summary(state, total_num, pages_done)

    print("\n[3/3] 处理完成")
    if state["stopped_subject"]:
        print("已按规则遇到首封满足条件的已读邮件后停止：「%s」"
              % state["stopped_subject"])
    else:
        print("[提醒] 未遇到满足条件的已读邮件，已处理完搜索范围内邮件。")
    print("共下载 %d 个PDF文件，目录：%s" % (len(state["downloaded"]), DOWNLOAD_DIR))
    for p in state["downloaded"]:
        print("  - %s" % os.path.basename(p))
    print("汇总文件：%s" % summary_path)


if __name__ == "__main__":
    main()
