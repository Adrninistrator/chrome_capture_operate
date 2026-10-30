# 企业微信消息：向脚本传递短信验证码、动态 Token

> 本文档**只**说明企业微信消息通道。该能力不写入 `api/api.md`、
> `api/auto-login-guide.md`、`api/browser-actions.md`；在生成脚本的提示词中
> 按需补充本节要求即可。

自动化脚本执行到需要人工提供**短信验证码、动态 Token、一次性口令、审批码**
等“只存在于用户手机上”的信息时，不必人工盯守，也不必暴露公网回调地址：

1. 脚本调用本服务发送一条企业微信消息（带 4 位消息序号）；
2. 用户在企业微信中直接回复内容；
3. 脚本轮询本服务，按消息序号取回回复，继续执行。

服务内部维护到企业微信智能机器人的 WSS 长连接，负责订阅鉴权、心跳保活、
断线重连和回复接收；回复保存在内存中，默认 5 分钟后清理。

---

## 1. 配置

在 Web 页面 **参数配置 → 企业微信消息** 中填写，保存到全局配置文件
`C:\Users\%username%\.chrome_capture_operate\global_conf.json` 的
`wecom_config`：

```json
"wecom_config": {
  "bot_id": "企业微信后台获取的机器人 ID",
  "secret": "dpapi:v1:Base64密文...",
  "chat_id": "单聊 userid 或群聊 chatid",
  "chat_type": 1,
  "reply_ttl": 300,
  "ws_url": ""
}
```

| 参数 | 说明 |
|---|---|
| `bot_id` | 企业微信智能机器人的 BotID（后台「API 模式 → 长连接」） |
| `secret` | 长连接专用密钥；**Windows DPAPI 用户级加密保存**，页面不回显，编辑留空保持原值 |
| `chat_id` | 接收消息的会话 ID：单聊填用户 `userid`，群聊填群 `chatid` |
| `chat_type` | `1` 单聊（默认）、`2` 群聊 |
| `reply_ttl` | 消息序号与回复在内存中的保存时长（秒），默认 `300`，最大 7 天 |
| `ws_url` | 长连接地址，留空使用官方 `wss://openws.work.weixin.qq.com`；本地联调可指向 mock 服务端 |

保存后长连接自动重启，无需重启主程序；重启会清空内存中尚未完成的序号与回复。
参数错误、密钥解密失败或订阅失败时，参数配置页会显示连接状态与最近错误。

**首次使用前提（企业微信官方限制）：**

- 用户需要先在企业微信里给机器人发过一条消息，机器人才能主动推送；
- 每个机器人同一时间只能保持一个有效长连接，本服务按单实例部署；
- 同一会话发送频率限制：30 条/分钟、1000 条/小时。

---

## 2. 发送消息

```http
POST /api/wecom/send_message
Content-Type: application/json
```

```json
{"seq": "0421", "content": "请提供登录短信验证码"}
```

| 字段 | 说明 |
|---|---|
| `seq` | **必填**，4 位数字字符串（`0000`–`9999`），由脚本生成，用于关联回复；在保存时长内不可重复 |
| `content` | **必填**，消息内容，UTF-8 不超过 20480 字节 |

服务会在 `content` 后自动追加回复提示（当前序号、直接回复即可、两行回复方式）。

成功：

```json
{"errcode": 0, "errmsg": "ok", "seq": "0421"}
```

| HTTP | 含义 |
|---|---|
| `200` | 发送成功 |
| `400` | `seq` 不是 4 位数字，或 `content` 为空/超过 20480 字节 |
| `409` | 该 `seq` 在保存时长内已被占用，换一个 |
| `502` | 企业微信返回非 0 错误码（如频率超限） |
| `503` | 未配置、长连接未就绪或已断开（稍后重试） |
| `500` | 内部异常 |

> 保存时长从**收到发送请求时**开始计算；发送失败会回滚登记并释放 `seq`。

---

## 3. 获取回复

```http
GET /api/wecom/get_reply?seq=0421
```

只接受消息序号；`seq` 必填且必须是 4 位数字字符串。

已收到回复：

```json
{"success": true, "message": "ok", "reply": "658231"}
```

尚未收到、序号已过期或不存在：

```json
{"success": false, "message": "暂无回复", "reply": null}
```

| HTTP | 含义 |
|---|---|
| `200` | 查询完成（看 `success` 判断是否有回复） |
| `400` | `seq` 缺失或不是 4 位数字 |
| `503` | 企业微信功能未配置 |

脚本侧只需判断 `success`：`true` 时取 `reply`；`false` 时继续轮询（建议间隔 1~2 秒，
并设置最大等待时长）。

---

## 4. 连接状态

```http
GET /api/wecom/status
```

```json
{
  "ok": true,
  "status": {
    "configured": true,
    "connected": true,
    "chat_id": "easonzheng",
    "chat_type": 1,
    "reply_ttl": 300,
    "active_count": 1,
    "last_error": ""
  }
}
```

`connected` 表示 WSS 已连接并完成订阅；`last_error` 为最近一次网络/订阅/发送错误
（不含密钥与回复内容）。

---

## 5. 配置接口（参数配置页使用）

```http
GET    /api/wecom/config
PUT    /api/wecom/config
DELETE /api/wecom/config
```

- GET 返回 `config`（`secret` 只返回 `secret_set` 布尔值）与 `status`；
- PUT 请求体同 `wecom_config` 字段；`secret` 留空表示保持原密文；
- DELETE 清除配置并停止长连接。

---

## 6. 用户怎么回复

| 方式 | 回复 | 归属 |
|---|---|---|
| 推荐：只发内容 | `658231` | 归属最近一条成功发送的消息 |
| 两行：首行序号 | `0421`（换行）`658231` | 归属序号 `0421` |
| 首行序号无效/已过期 | 如上 | 取第二行内容，按最近一条消息处理 |

消息末尾已内置操作提示；请提示用户**不要引用原消息**再回复。

只有文本（含 markdown 文本）消息会被处理；图片、文件、语音等忽略。
企业微信可能重复推送同一消息，服务按 `msgid` 去重。

---

## 7. 脚本示例（Python）

```python
import random
import time
import requests

SERVICE = "http://127.0.0.1:33445"
HTTP = requests.Session()
HTTP.trust_env = False          # 避免系统代理拦截 127.0.0.1


def ask_wecom(title, timeout=300, interval=2):
    """发送企业微信消息并等待用户回复，返回回复内容。"""
    # 4 位序号由调用方生成；409 表示冲突，换一个重试
    for _ in range(10):
        seq = "%04d" % random.randint(0, 9999)
        r = HTTP.post(SERVICE + "/api/wecom/send_message",
                      json={"seq": seq,
                            "content": "%s（请直接回复内容）" % title},
                      timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("errcode") == 0:
            break
        if r.status_code != 409:
            raise RuntimeError("发送企业微信消息失败: %s" % data)
    else:
        raise RuntimeError("消息序号连续冲突")

    deadline = time.time() + timeout
    while time.time() < deadline:
        reply = HTTP.get(SERVICE + "/api/wecom/get_reply",
                         params={"seq": seq}, timeout=10).json()
        if reply.get("success"):
            return reply["reply"]
        time.sleep(interval)
    raise TimeoutError("等待「%s」回复超时" % title)


# 在脚本中：
# code = ask_wecom("登录短信验证码", timeout=300)
# code 即为用户在企业微信中回复的验证码
```

Shell / 其他语言同理：发送后按 `seq` 轮询 `get_reply`，判断 `success`。
调用方必须设置最大等待时长，避免用户不回复导致流程卡死。

---

## 8. 在 AI 生成脚本时使用

默认的 **d. 自动登录脚本提示词** 模板**不包含**企业微信功能。需要时：

1. 在 **参数配置 → 企业微信消息** 完成机器人配置，并确认状态为“长连接已连接”；
2. 在 d 页面的提示词中补充要求，例如：
   - “遇到短信验证码/动态 Token 时，调用 `POST /api/wecom/send_message`
     发送通知（seq 使用 4 位随机数字），然后轮询
     `GET /api/wecom/get_reply?seq=...` 获取用户回复，超时 5 分钟；”
   - “企业微信接口说明见 `api/wecom-message.md`”；
3. AI 生成的脚本只依赖 `requests`，不需要 MCP/Playwright。

---

## 9. 限制与安全

- **单实例**：每个机器人只能保持一个长连接；不要同时运行多个本服务连接同一 `bot_id`；
- **内存存储**：服务重启后未完成的消息与回复丢失；单次等待建议 2~5 分钟，
  需要更长时同步调大 `reply_ttl`；
- **频率限制**：同一会话 30 条/分钟、1000 条/小时，不适合大批量推送；
- **内容只是通道**：验证码/Token 会出现在用户企业微信中，请勿发送与获取无关的敏感信息；
- **日志**：服务日志**不记录**回复内容、消息正文与密钥，只记录序号与长度；
  `wecom_config.secret` 使用 Windows DPAPI 用户级加密，换 Windows 用户/机器后
  无法解密，需要在参数配置页重新填写；
- **安全边界**：接口只监听 `127.0.0.1`，与主应用其他接口共用端口；不要将服务
  暴露到公网。

---

## 10. 与参考项目的关系

本功能参考并移植自 `wecom-aibot/send_recv_wecom_msg`（原为独立 aiohttp 服务 +
`config.toml`）。移植差异：

- HTTP 接口并入本项目 FastAPI，路径统一为 `/api/wecom/*`；
- 配置改存全局配置文件 `wecom_config`，由参数配置页维护，`secret` 改为 DPAPI 加密；
- 长连接随主应用生命周期启动/停止，配置保存后自动重启；
- 不提供 `detail=true` 原始事件帧返回，避免把用户消息原文透出；
- 回复内容不写日志。