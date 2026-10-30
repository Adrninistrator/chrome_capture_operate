# -*- coding: utf-8 -*-
"""企业微信消息通道：脚本向用户索取短信验证码 / 动态 Token 等一次性信息。

参考实现：``D:\\gitee-dir\\pri-code\\wecom-aibot\\send_recv_wecom_msg``
（已按本项目架构移植：HTTP 接口并入 FastAPI，配置改存全局配置文件）。

能力：

1. 后台 WSS 长连接（企业微信智能机器人「API 模式 → 长连接」）：
   订阅鉴权、应用层心跳、断线重连、用户回复实时接收；
2. 对外 HTTP 接口（见 ``api/wecom-message.md``）：
   - 发送消息：消息内容 + 4 位消息序号；
   - 获取回复：只按消息序号查询；
3. 回复按消息序号归属，保存在内存（默认 5 分钟），进程重启即丢失。

配置保存在全局配置文件 ``wecom_config``（参数配置页维护），其中
``secret`` 使用 Windows DPAPI 用户级加密。企业微信官方限制：

- 每个机器人同一时间只能保持一个有效长连接（单实例部署）；
- 用户需要先给机器人发过消息，机器人才能主动推送；
- 同一会话发送频率 30 条/分钟、1000 条/小时。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

import websockets

from . import globalconf
from . import secret_store

log = logging.getLogger("app.wecom")

WSS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 30       # 心跳间隔（秒）
REQUEST_TIMEOUT = 10          # 等待响应的超时（秒）
INITIAL_RECONNECT_DELAY = 1   # 网络异常初始重连延迟（秒）
MAX_RECONNECT_DELAY = 30      # 网络异常重连退避上限（秒）
SUBSCRIPTION_ERROR_DELAY = 30       # 订阅失败初始退避（秒）
MAX_SUBSCRIPTION_ERROR_DELAY = 300  # 订阅失败退避上限（秒）
MSGID_CACHE_SIZE = 1000             # 消息 msgid 去重缓存条数上限
MAX_CONTENT_BYTES = 20480            # 企微 markdown 内容长度上限（UTF-8 字节）
SEQ_PATTERN = re.compile(r"^[0-9]{4}$")  # 消息序号：4 位 ASCII 数字

MAX_FIELD_LEN = 500           # bot_id / chat_id / secret / ws_url 长度上限


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class WecomConfig:
    bot_id: str
    secret: str
    chat_id: str
    chat_type: int = 1       # 1: 单聊（userid） 2: 群聊（chatid）
    reply_ttl: int = 300     # 内存中消息数据的保存时长（秒）
    ws_url: str = WSS_URL    # 长连接地址（本地联调可指向 mock 服务端）


class WecomConfigError(RuntimeError):
    """企业微信配置缺失/非法/保存失败"""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NotConfiguredError(RuntimeError):
    """企业微信功能未配置"""


class NotConnectedError(RuntimeError):
    """WSS 尚未连接 / 已断开"""


class WecomError(RuntimeError):
    """企业微信返回了非 0 错误码"""


class SubscriptionError(WecomError):
    """订阅（aibot_subscribe）失败：凭证错误、旧连接占用或频率保护"""

    def __init__(self, errcode, errmsg):
        super().__init__("订阅失败: errcode=%s, errmsg=%s" % (errcode, errmsg))
        self.errcode = errcode
        self.errmsg = errmsg


class SeqConflictError(RuntimeError):
    """消息序号在保存时长内已被占用"""

    def __init__(self, seq):
        super().__init__("消息序号 %s 已被占用" % seq)
        self.seq = seq


def _raw_config():
    v = globalconf.get_json(globalconf.KEY_WECOM_CONFIG, {})
    return v if isinstance(v, dict) else {}


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _secret_is_encrypted(value):
    return secret_store.is_encrypted(str(value or ""))


def _decrypt_secret(value):
    value = str(value or "")
    if not value:
        return ""
    if _secret_is_encrypted(value):
        return secret_store.unprotect(value)
    # 兼容人工直接写全局配置的明文密钥（不推荐）
    return value


def read_config():
    """读取全局配置并解密；未配置返回 None，配置不完整/非法抛异常。"""
    raw = _raw_config()
    if not raw:
        return None
    bot_id = str(raw.get("bot_id") or "").strip()
    chat_id = str(raw.get("chat_id") or "").strip()
    stored_secret = str(raw.get("secret") or "").strip()
    if not (bot_id or chat_id or stored_secret):
        return None
    if not (bot_id and chat_id and stored_secret):
        raise WecomConfigError(
            "企业微信配置不完整：bot_id / secret / chat_id 均不能为空")
    try:
        secret = _decrypt_secret(stored_secret)
    except secret_store.SecretStoreError as e:
        raise WecomConfigError("企业微信 secret 解密失败：%s" % e) from e
    if not secret:
        raise WecomConfigError("企业微信 secret 为空")

    chat_type = _as_int(raw.get("chat_type"), 1)
    if chat_type not in (1, 2):
        raise WecomConfigError("chat_type 只能是 1（单聊）或 2（群聊）")
    reply_ttl = _as_int(raw.get("reply_ttl"), 300)
    if reply_ttl <= 0:
        raise WecomConfigError("reply_ttl 必须是大于 0 的整数秒")
    ws_url = str(raw.get("ws_url") or "").strip() or WSS_URL
    if not ws_url.startswith(("ws://", "wss://")):
        raise WecomConfigError("ws_url 必须以 ws:// 或 wss:// 开头")
    return WecomConfig(bot_id=bot_id, secret=secret, chat_id=chat_id,
                       chat_type=chat_type, reply_ttl=reply_ttl,
                       ws_url=ws_url)


def public_config():
    """配置的对外展示（secret 不返回明文，只返回是否已设置）。"""
    raw = _raw_config()
    bot_id = str(raw.get("bot_id") or "")
    chat_id = str(raw.get("chat_id") or "")
    return {
        "configured": bool(bot_id and chat_id and raw.get("secret")),
        "bot_id": bot_id,
        "chat_id": chat_id,
        "chat_type": _as_int(raw.get("chat_type"), 1),
        "reply_ttl": _as_int(raw.get("reply_ttl"), 300),
        "ws_url": str(raw.get("ws_url") or ""),
        "secret_set": bool(raw.get("secret")),
    }


def _validate_text(name, value, allow_empty=False):
    value = str(value or "").strip()
    if not value and not allow_empty:
        raise WecomConfigError("%s 不能为空" % name)
    if len(value) > MAX_FIELD_LEN:
        raise WecomConfigError("%s 过长（最多 %d 个字符）"
                               % (name, MAX_FIELD_LEN))
    if "\r" in value or "\n" in value:
        raise WecomConfigError("%s 不能包含换行" % name)
    return value


def save_config(data):
    """校验并保存配置到全局配置；返回 (WecomConfig)。

    ``secret`` 留空表示保持原密钥（与登录账号密码类型同口径）；
    首次配置必须填写。不提供清空语义（清空用 clear_config）。
    """
    if not isinstance(data, dict):
        raise WecomConfigError("配置格式错误")
    bot_id = _validate_text("bot_id", data.get("bot_id"))
    chat_id = _validate_text("chat_id", data.get("chat_id"))
    chat_type = _as_int(data.get("chat_type"), 1)
    if chat_type not in (1, 2):
        raise WecomConfigError("chat_type 只能是 1（单聊）或 2（群聊）")
    reply_ttl = _as_int(data.get("reply_ttl"), 300)
    if reply_ttl <= 0:
        raise WecomConfigError("reply_ttl 必须是大于 0 的整数秒")
    if reply_ttl > 604800:
        raise WecomConfigError("reply_ttl 不能超过 7 天（604800 秒）")
    ws_url = _validate_text("ws_url", data.get("ws_url"), allow_empty=True)
    if ws_url and not ws_url.startswith(("ws://", "wss://")):
        raise WecomConfigError("ws_url 必须以 ws:// 或 wss:// 开头")

    raw = _raw_config()
    existing_secret = str(raw.get("secret") or "").strip()
    secret_input = str(data.get("secret") or "").strip()
    if secret_input:
        if len(secret_input) > MAX_FIELD_LEN:
            raise WecomConfigError(
                "secret 过长（最多 %d 个字符）" % MAX_FIELD_LEN)
        try:
            stored_secret = secret_store.protect(secret_input)
        except secret_store.SecretStoreError as e:
            raise WecomConfigError("企业微信 secret 加密失败：%s" % e,
                                   status_code=500) from e
    elif existing_secret:
        stored_secret = existing_secret
    else:
        raise WecomConfigError("首次配置必须填写 secret")

    stored = {
        "bot_id": bot_id,
        "secret": stored_secret,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "reply_ttl": reply_ttl,
        "ws_url": ws_url,
    }
    if not globalconf.set_json(globalconf.KEY_WECOM_CONFIG, stored):
        raise WecomConfigError("写入全局配置文件失败", status_code=500)
    log.info("企业微信配置已保存: chat_id=%s chat_type=%d ttl=%d",
             chat_id, chat_type, reply_ttl)
    return read_config()


def clear_config():
    """清除配置（停用功能）；WSS 由调用方停止。"""
    if not globalconf.set_json(globalconf.KEY_WECOM_CONFIG, {}):
        raise WecomConfigError("写入全局配置文件失败", status_code=500)
    log.info("企业微信配置已清除")


# --------------------------------------------------------------------------- #
# 消息内容校验 / 拼接 / 解析
# --------------------------------------------------------------------------- #
def validate_seq(seq):
    if not isinstance(seq, str) or not SEQ_PATTERN.match(seq):
        raise ValueError("seq 必填，且必须是 4 位数字字符串（0000–9999）")
    return seq


def validate_content(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content 必填且不能为空")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("content 超过 %d 字节" % MAX_CONTENT_BYTES)
    return content


def build_outgoing_message(content, seq):
    """调用方内容 + 带粗体强调的回复提示语。"""
    return (
        "%s\n\n"
        "---\n"
        "当前消息序号：**%s**\n\n"
        "**直接回复即可**，只需发送你的内容。\n\n"
        "也可以分两行回复：\n"
        "第一行只填序号 **%s**\n"
        "第二行写你的内容\n\n"
        "请不要引用本条消息。"
    ) % (content, seq, seq)


def extract_text(frame):
    """从事件帧中提取用户消息文本，兼容 text / markdown 等常见结构。"""
    if not isinstance(frame, dict):
        return None
    body = frame.get("body")
    if not isinstance(body, dict):
        return None
    msgtype = body.get("msgtype")
    if msgtype == "text":
        return (body.get("text") or {}).get("content")
    if msgtype == "markdown":
        return (body.get("markdown") or {}).get("content")
    text = (body.get("text") or {}).get("content")
    if isinstance(text, str) and text:
        return text
    content = body.get("content")
    if isinstance(content, str) and content:
        return content
    return None


def parse_reply(text):
    """解析回复文本：两行且首行为 4 位数字 -> (序号, 内容)；否则 (None, 原文)。"""
    text = (text or "").strip()
    lines = text.splitlines()
    if len(lines) >= 2:
        first = lines[0].strip()
        if SEQ_PATTERN.match(first):
            return first, "\n".join(lines[1:]).strip()
    return None, text


# --------------------------------------------------------------------------- #
# 内存存储：消息序号 -> 回复内容（带 TTL 自动过期）
# --------------------------------------------------------------------------- #
class MessageStore:
    """内存状态：消息序号 → 回复内容，条目按 TTL 自动过期。"""

    def __init__(self, ttl=300):
        self.ttl = ttl
        self._current_seq = None
        self._entries = {}

    @property
    def current_seq(self):
        return self._current_seq

    def is_active(self, seq):
        entry = self._entries.get(seq)
        if entry is None:
            return False
        if time.time() >= entry["expire_at"]:
            self._remove(seq)
            return False
        return True

    def register(self, seq):
        if self.is_active(seq):
            raise SeqConflictError(seq)
        self._entries[seq] = {"expire_at": time.time() + self.ttl,
                              "reply": None}

    def set_current(self, seq):
        self._current_seq = seq

    def save_reply(self, content, seq=None):
        """保存回复到指定序号（缺省当前序号）；序号无效返回 None。"""
        target = seq if seq is not None else self._current_seq
        if target is None or not self.is_active(target):
            return None
        self._entries[target]["reply"] = {
            "content": content,
            "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return target

    def get_reply(self, seq):
        if not self.is_active(seq):
            return None
        return self._entries[seq]["reply"]

    def remove(self, seq):
        self._remove(seq)

    def _remove(self, seq):
        self._entries.pop(seq, None)
        if self._current_seq == seq:
            self._current_seq = None

    def purge_expired(self, now=None):
        now = time.time() if now is None else now
        expired = [seq for seq, entry in self._entries.items()
                   if now >= entry["expire_at"]]
        for seq in expired:
            self._entries.pop(seq, None)
            if self._current_seq == seq:
                self._current_seq = None
        return len(expired)

    def active_count(self):
        self.purge_expired()
        return len(self._entries)

    async def cleanup_loop(self, interval):
        while True:
            await asyncio.sleep(interval)
            removed = self.purge_expired()
            if removed:
                log.info("已清理过期企业微信消息数据: %s 条", removed)

    def clear(self):
        self._current_seq = None
        self._entries.clear()


# --------------------------------------------------------------------------- #
# WSS 长连接客户端
# --------------------------------------------------------------------------- #
class WecomWssClient:
    """连接、订阅、心跳、重连，以及请求/响应配对和事件分发。"""

    def __init__(self, config, store):
        self.config = config
        self.store = store
        self._ws = None
        self._pending = {}
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._task = None
        self._seen_msgids = OrderedDict()
        self.last_error = ""

    @property
    def connected(self):
        return self._connected.is_set()

    # ---------- 生命周期 ----------
    async def start(self):
        self._task = asyncio.create_task(self._run_forever(),
                                         name="wecom-wss")

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run_forever(self):
        network_delay = INITIAL_RECONNECT_DELAY
        subscribe_delay = SUBSCRIPTION_ERROR_DELAY
        while not self._stop.is_set():
            try:
                await self._session()
                log.info("企业微信 WSS 连接已结束，准备重连")
                network_delay = INITIAL_RECONNECT_DELAY
                subscribe_delay = SUBSCRIPTION_ERROR_DELAY
            except asyncio.CancelledError:
                raise
            except SubscriptionError as exc:
                self.last_error = str(exc)
                log.warning("企业微信订阅失败: errcode=%s, errmsg=%s",
                            exc.errcode, exc.errmsg)
                if self._stop.is_set():
                    break
                log.info("%s 秒后重新订阅", subscribe_delay)
                await asyncio.sleep(subscribe_delay)
                subscribe_delay = min(subscribe_delay * 2,
                                      MAX_SUBSCRIPTION_ERROR_DELAY)
                continue
            except Exception as exc:
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                log.warning("企业微信 WSS 异常: %s: %s",
                            type(exc).__name__, exc)

            if self._stop.is_set():
                break
            log.info("%s 秒后重连企业微信 WSS", network_delay)
            await asyncio.sleep(network_delay)
            network_delay = min(network_delay * 2, MAX_RECONNECT_DELAY)

    async def _session(self):
        # 企微服务端不兼容协议层 Ping 控制帧，保活统一使用应用层 cmd=ping
        async with websockets.connect(self.config.ws_url,
                                      ping_interval=None) as ws:
            self._ws = ws
            log.info("企业微信 WSS 已连接: %s", self.config.ws_url)
            tasks = [asyncio.create_task(self._recv_loop(ws), name="recv")]
            try:
                resp = await self._request(ws, "aibot_subscribe", {
                    "bot_id": self.config.bot_id,
                    "secret": self.config.secret,
                })
                if resp.get("errcode") != 0:
                    raise SubscriptionError(resp.get("errcode"),
                                            resp.get("errmsg"))
                self.last_error = ""
                self._connected.set()
                log.info("企业微信订阅成功")
                tasks.append(asyncio.create_task(
                    self._heartbeat_loop(ws), name="heartbeat"))
                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                self._connected.clear()
                self._ws = None
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat_loop(self, ws):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            resp = await self._request(ws, "ping")
            if resp.get("errcode") != 0:
                raise WecomError("心跳失败: errcode=%s, errmsg=%s"
                                 % (resp.get("errcode"),
                                    resp.get("errmsg")))

    async def _recv_loop(self, ws):
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("企业微信收到非 JSON 数据: %s", str(raw)[:200])
                continue
            headers = frame.get("headers") if isinstance(frame, dict) else None
            req_id = headers.get("req_id") if isinstance(headers, dict) else None
            future = self._pending.get(req_id)
            is_response = isinstance(frame, dict) and "cmd" not in frame
            if is_response and future is not None and not future.done():
                future.set_result(frame)
            else:
                self._on_event(frame)

    def _is_duplicate_msgid(self, msgid):
        if msgid in self._seen_msgids:
            return True
        self._seen_msgids[msgid] = None
        if len(self._seen_msgids) > MSGID_CACHE_SIZE:
            self._seen_msgids.popitem(last=False)
        return False

    def _on_event(self, frame):
        body = frame.get("body") if isinstance(frame, dict) else None
        msgid = body.get("msgid") if isinstance(body, dict) else None
        if msgid and self._is_duplicate_msgid(msgid):
            log.info("企业微信收到重复消息 msgid=%s，已忽略", msgid)
            return
        raw_text = extract_text(frame)
        if raw_text is None:
            log.info("企业微信收到非消息事件（已忽略）")
            return
        seq, content = parse_reply(raw_text)
        target = seq if (seq is not None and self.store.is_active(seq)) else None
        if seq is not None and target is None:
            log.warning("企业微信回复中的消息序号 %s 不存在或已过期，按单行处理",
                        seq)
        saved = self.store.save_reply(content, target)
        if saved is None:
            log.warning("企业微信收到消息但当前没有有效的消息序号，已忽略")
        else:
            # 不记录回复内容（可能是验证码/Token），只记录归属与长度
            log.info("企业微信回复已保存: seq=%s 长度=%d", saved, len(content))

    async def _request(self, ws, cmd, body=None, timeout=REQUEST_TIMEOUT):
        req_id = uuid.uuid4().hex
        frame = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            frame["body"] = body
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            async with self._send_lock:
                await ws.send(json.dumps(frame, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def send_message(self, content):
        """向配置的 chat_id 发送一条 markdown 消息。"""
        ws = self._ws
        if ws is None or not self._connected.is_set():
            raise NotConnectedError("企业微信长连接尚未就绪")
        try:
            resp = await self._request(ws, "aibot_send_msg", {
                "chatid": self.config.chat_id,
                "chat_type": self.config.chat_type,
                "msgtype": "markdown",
                "markdown": {"content": content},
            })
        except websockets.ConnectionClosed as exc:
            raise NotConnectedError("企业微信长连接已断开: %s" % exc) from exc
        if resp.get("errcode") != 0:
            raise WecomError("发送失败: errcode=%s, errmsg=%s"
                             % (resp.get("errcode"), resp.get("errmsg")))
        return resp


# --------------------------------------------------------------------------- #
# 服务管理（随主应用生命周期启动/停止，配置变更时重启长连接）
# --------------------------------------------------------------------------- #
class WecomService:
    """企业微信消息服务：持有配置、内存存储与长连接。"""

    def __init__(self):
        self.config = None
        self.store = None
        self.client = None
        self.last_error = ""
        self._cleanup_task = None
        self._apply_lock = asyncio.Lock()

    async def start(self):
        try:
            cfg = read_config()
        except (WecomConfigError, secret_store.SecretStoreError) as e:
            self.last_error = str(e)
            log.warning("企业微信配置无效，功能未启动: %s", e)
            return
        await self._apply(cfg)

    async def stop(self):
        async with self._apply_lock:
            await self._stop_current()
            self.config = None

    async def apply_config(self, cfg):
        """应用新配置：重启长连接；cfg 为 None 时停用。"""
        async with self._apply_lock:
            await self._stop_current()
            self.last_error = ""
            self.config = cfg
            if cfg is None:
                return
            self.store = MessageStore(cfg.reply_ttl)
            self.client = WecomWssClient(cfg, self.store)
            await self.client.start()
            interval = max(10, min(cfg.reply_ttl // 10, 60))
            self._cleanup_task = asyncio.create_task(
                self.store.cleanup_loop(interval), name="wecom-cleanup")
            log.info("企业微信消息服务已启动: chat_id=%s chat_type=%d "
                     "reply_ttl=%d cleanup=%ds", cfg.chat_id, cfg.chat_type,
                     cfg.reply_ttl, interval)

    async def _apply(self, cfg):
        await self.apply_config(cfg)

    async def _stop_current(self):
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        if self.client is not None:
            await self.client.stop()
            self.client = None
        self.store = None

    def status(self):
        client = self.client
        cfg = self.config
        return {
            "configured": cfg is not None,
            "connected": bool(client and client.connected),
            "chat_id": cfg.chat_id if cfg else "",
            "chat_type": cfg.chat_type if cfg else 1,
            "reply_ttl": cfg.reply_ttl if cfg else 0,
            "active_count": self.store.active_count() if self.store else 0,
            "last_error": ((client.last_error if client else "")
                           or self.last_error),
        }

    async def send(self, seq, content):
        """登记序号并发送消息；失败回滚序号。返回成功响应 dict。"""
        if self.config is None or self.client is None or self.store is None:
            raise NotConfiguredError(
                "企业微信未配置（请在“参数配置”页填写机器人参数）")
        self.store.register(seq)
        previous_seq = self.store.current_seq
        self.store.set_current(seq)
        try:
            await self.client.send_message(
                build_outgoing_message(content, seq))
        except Exception:
            self.store.remove(seq)
            self.store.set_current(previous_seq)
            raise
        log.info("企业微信消息已发送: seq=%s 长度=%d", seq, len(content))
        return {"errcode": 0, "errmsg": "ok", "seq": seq}

    def get_reply(self, seq):
        if self.store is None:
            raise NotConfiguredError(
                "企业微信未配置（请在“参数配置”页填写机器人参数）")
        item = self.store.get_reply(seq)
        if item is None:
            return {"success": False, "message": "暂无回复", "reply": None}
        return {"success": True, "message": "ok",
                "reply": item["content"]}