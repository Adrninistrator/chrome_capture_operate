# Cookie、Authorization 获取接口说明（api.md）

本文档面向**生成的 Python 脚本的编写者（人或 AI）**：说明如何通过
chrome_capture_operate 的 HTTP 接口获取指定网站的 Cookie 与
Authorization，以及生成脚本的约定。

服务默认监听 `http://127.0.0.1:33445`（仅本机；端口以参数配置页实际值为准）。

## 1. 获取指定网址的 Cookie、Authorization

本节接口获取的 Cookie、Authorization 来自**日常使用的 Chrome**（Chrome 插件按启动/变更/定时推送到服务，仅存内存）。

```
GET /api/cookies/query?url=<URL或域名>[&profile=<Chrome实例编号>]
```

- `url`：完整 URL（如 `https://www.example.com/api/data`）或域名/IP，需 URL 编码。
- `profile`：Chrome 实例编号（多开区分，可选，默认 0 查所有实例——
  查到即返回）。非 0 时仅在该编号实例的 Cookie 中查找；脚本需要
  用特定账号登录态时传对应编号（编号见 Web 页面"Chrome进程多开"列表）。

### 返回的 Cookie 范围（匹配规则）

按**浏览器语义**：返回"浏览器访问该主机时真实会带的所有 cookie"，即：

- Cookie 的 `domain` 为 `.example.com` 形式时，匹配主域及所有子域
  （`example.com`、`www.example.com`、`api.example.com` 均命中）；
- Cookie 的 `domain` 无前置点（host-only，如 `www.example.com`）时，仅精确匹配该主机；
- Authorization（可选字段）：按 Cookie 请求的**同一主机精确匹配**——
  Authorization 是站点自己设置的请求头（不跨域携带），与 Cookie 的
  后缀匹配语义不同，`www.example.com` 的 Authorization 不会匹配
  `api.example.com`。

### 成功响应（200）

```json
{
  "ok": true,
  "cookies": [
    {"name": "session_id", "value": "xxx", "domain": ".example.com",
     "path": "/", "secure": true, "httpOnly": true, "expires": 1780000000}
  ],
  "cookie_header": "session_id=xxx; theme=dark",
  "authorizations": [{"host": "www.example.com", "value": "Bearer t"}],
  "authorization": "Bearer t"
}
```

- `cookies`：完整字段，按需构造 `requests` 的 cookie jar；
- `cookie_header`：已拼好的 `Cookie` 请求头字符串，可直接放进 headers；
- `authorizations`：命中的 Authorization 列表（无匹配为空数组），
  `host` 为匹配主机；
- `authorization`：已可直接使用的 `Authorization` 请求头值（无匹配为空串），
  直接放进 headers 即可。

> 分区 Cookie（Partitioned/CHIPS）：登录流程以分区形式写入的会话
> cookie 由插件按 `partitionKey.topLevelSite` 补读推送。同名 cookie 存在
> 多个分区变体时，查询按"分区站点=查询主机 > 未分区 > 其他分区"取一条，
> `cookies` 条目中的 `partitionSite` 字段即其分区站点（未分区为空串）。

### 失败响应（404，返回明确错误）

```json
{"ok": false, "error": "没有与主机 www.example.com 匹配的 Cookie（插件可能尚未推送该网站的 Cookie；若登录态在用于抓包的 Chrome 中，可改用 /api/cookies/cdp 实时读取）"}
```

| 场景 | 错误信息要点 |
|---|---|
| 缺少/无法解析 url 参数 | 缺少 url 参数 / 无法解析主机名 |
| 从未收到插件推送 | 提示确认插件已安装、目标地址正确、已成功推送 |
| 无匹配该主机的 Cookie | 提示插件可能尚未推送该网站；若登录态在用于抓包的 Chrome 中，提示改用 `/api/cookies/cdp`（见第 2 节） |

调用方应将非 200 响应视为"未获取到 Cookie"，打印 `error` 后退出或重试。

> 404 判定口径：cookie 与 Authorization **均**无匹配才返回 404；任一命中
> 即 200（纯 Authorization 站点的 `cookies` 为空数组）。向下兼容：只读
> `cookies`/`cookie_header` 的旧脚本行为不变。
>
> Authorization 的来源：Chrome 插件观察浏览器实际请求头中的 Authorization
> （范围内主机），与 Cookie 同一推送链路、同一接收页面展示。

## 2. 获取用于抓包的 Chrome 的 Cookie、Authorization（实时）

由 AI 分析并生成脚本时，有可能需要获得 Cookie 发起实际 HTTP/HTTPS 请求以进行验证；抓包文件中的 Cookie、Authorization 值均掩码，无法从抓包数据获取。以下接口经 CDP 实时读取**用于抓包的 Chrome** 内存中的 Cookie（含 httpOnly 与分区 Cookie），仅在内存中传递，不长期保存到文件。

```
GET /api/cookies/cdp?url=<URL或域名>[&port=<CDP端口>]
```

- `url`：必填，与 `/api/cookies/query` 同口径——完整 URL 或域名/IP；Cookie 按其主机名做域名匹配（`domain` 带前置点匹配主域及所有子域，host-only 精确匹配），Authorization 按其主机名精确匹配
- `port`：可选，默认取配置的 CDP 调试端口（用于抓包的 Chrome 实例）；仅影响 Cookie 读取，Authorization 观察来自配置实例的抓包事件流

**Authorization 的获取机制（观察缓存）**：Authorization 是站点 JS/Service Worker 发请求时设置的请求头，不存在于 Cookie 存储，只能观察实际请求。抓包期间服务观察请求头中的 Authorization，按主机缓存于内存（与插件观察 Authorization 同口径）。因此需要**先开始抓包再操作页面**，停止抓包后缓存仍在内存中，脚本仍可查询；服务重启后清空。

### 成功响应（200）

```json
{
  "ok": true,
  "source": "cdp",
  "cdp_port": 9222,
  "cookies": [
    {"name": "JSESSIONID", "value": "xxx", "domain": "uat.cmdb.weoa.com",
     "path": "/", "secure": false, "httpOnly": true, "expires": -1,
     "partitionSite": ""}
  ],
  "cookie_header": "JSESSIONID=xxx",
  "authorizations": [
    {"host": "api.example.com", "value": "Bearer eyJ...", "observed_at": 1790477584.66}
  ],
  "authorization": "Bearer eyJ...",
  "cookie_error": ""
}
```

- 字段名与 `/api/cookies/query` 完全对齐，按现有方式解析不受影响；新增 `source`/`cdp_port` 标识字段
- `observed_at`：Authorization 的观察时刻（epoch 秒），供判断凭据新鲜度；无匹配为空数组/空串（字段保留）
- `cookie_error`：仅当 Authorization 命中但 Cookie 读取失败时非空，透出 Cookie 侧的失败原因

### 失败响应（404，返回明确错误）

- 与 `/api/cookies/query` 兼容口径：Cookie 与 Authorization **均**无命中才返回 404，任一命中即 200（纯 Authorization 命中时 `cookies` 为空数组，`cookie_error` 说明 Cookie 侧原因）
- 用于抓包的 Chrome 未启动 / CDP 端口不可达（且无 Authorization 命中）：`CDP 读取失败（抓包 Chrome 未启动或调试端口 xx 不可达）: <底层错误>`
- 主机无任何匹配：`没有与主机 xx 匹配的 Cookie（抓包 Chrome 中可能未登录该站点）`
- `url` 无法解析主机名：`缺少 url 参数或无法解析主机名`；`url` 完全缺失时由接口框架参数校验直接返回 422（与 `/api/cookies/query` 行为一致）

### 与 /api/cookies/query 的关系（互补，非替代）

| | /api/cookies/query | /api/cookies/cdp |
|---|---|---|
| Cookie 来源 | 插件推送的快照（日常 Chrome） | 抓包 Chrome 实时内存（Storage.getCookies） |
| Cookie 时效 | 定时推送间隔 + 整体覆盖 | 实时（登录后立即可查） |
| Cookie 能力边界 | 受插件清单与 chrome.cookies API 限制（分区 Cookie 不可见） | httpOnly、CHIPS 分区 Cookie 都可读 |
| Authorization | 插件观察日常 Chrome 的请求头 | 抓包期间观察抓包 Chrome 的请求头 |

日常 Chrome 未开 CDP 调试端口时依旧只能使用 `/api/cookies/query`（插件链路）。

## 3. 生成的脚本的编写约定

1. **执行环境**：脚本由本项目的 Python 虚拟环境执行（`.venv`），
   运行方式为 `python -u 脚本路径`，**不传递任何参数**。
2. **依赖**：优先使用 `requests`（已随工具安装在虚拟环境中）；
   如需其他虚拟环境中不存在的第三方库，提醒人工将其加入
   `chrome_capture_operate_python/requirements.txt` 并重新运行
   `install.bat` 安装。
3. **输出**：脚本需要在 stdout 打印必要的信息（执行进度、关键结果、错误原因），
   Web 页面读取 stdout/stderr 展示。经 Web 页面、定时、系统托盘执行时工具
   已注入 UTF-8 输出编码；**直接在命令行运行**时 Windows 下 Python 默认按
   GBK 编码 stdout，中文输出会乱码，脚本开头固定重配置 stdout/stderr 为
   UTF-8（写法见第 4 节骨架）。
4. **HTTPS 证书**：发送 HTTPS 请求时**不要进行证书等任何验证**
   （`requests` 传 `verify=False`），以兼容内部系统的自签/内网证书。

## 4. 脚本示例骨架

```python
import sys
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COOKIE_API = "http://127.0.0.1:33445/api/cookies/query"
COOKIE_API_CDP = "http://127.0.0.1:33445/api/cookies/cdp"
TARGET = "https://www.example.com/api/data"

def get_auth_headers(url):
    """取 Cookie 与 Authorization 请求头（其一为空即不带）。

    优先日常 Chrome 插件推送的快照（query），无匹配时降级经 CDP 实时
    读取用于抓包的 Chrome（cdp）——登录态在用于抓包的 Chrome 中时走
    该路径。
    """
    last_err = ""
    for api in (COOKIE_API, COOKIE_API_CDP):
        try:
            r = requests.get(api, params={"url": url},
                             timeout=10, verify=False)
        except requests.RequestException as e:
            print("Cookie 服务不可达（chrome_capture_operate 未启动？）:", e)
            sys.exit(1)
        if r.status_code == 200:
            d = r.json()
            headers = {}
            if d.get("cookie_header"):
                headers["Cookie"] = d["cookie_header"]
            if d.get("authorization"):
                headers["Authorization"] = d["authorization"]
            if headers:
                return headers
        else:
            last_err = r.json().get("error", "")
    print("获取 Cookie 失败（日常 Chrome 插件与用于抓包的 Chrome 均无该"
          "站点登录态）:", last_err)
    sys.exit(1)

def main():
    resp = requests.get(TARGET, headers=get_auth_headers(TARGET),
                        timeout=30, verify=False)
    print("状态码:", resp.status_code)
    print(resp.text[:2000])

if __name__ == "__main__":
    main()
```
