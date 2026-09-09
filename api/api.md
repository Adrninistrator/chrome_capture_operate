# Cookie、Authorization 获取接口说明（api.md）

本文档面向**固化 Python 脚本的编写者（人或 AI）**：说明如何通过
chrome_capture_operate 的 HTTP 接口获取指定网站的 Cookie 与
Authorization，以及生成脚本的约定。

服务默认监听 `http://127.0.0.1:33445`（仅本机；端口以参数配置页实际值为准）。

## 1. 获取指定网址的 Cookie、Authorization

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
{"ok": false, "error": "没有与主机 www.example.com 匹配的 Cookie（插件可能尚未推送该网站的 Cookie）"}
```

| 场景 | 错误信息要点 |
|---|---|
| 缺少/无法解析 url 参数 | 缺少 url 参数 / 无法解析主机名 |
| 从未收到插件推送 | 提示确认插件已安装、目标地址正确、已成功推送 |
| 无匹配该主机的 Cookie | 提示插件可能尚未推送该网站 |

调用方应将非 200 响应视为"未获取到 Cookie"，打印 `error` 后退出或重试。

> 404 判定口径：cookie 与 Authorization **均**无匹配才返回 404；任一命中
> 即 200（纯 Authorization 站点的 `cookies` 为空数组）。向下兼容：只读
> `cookies`/`cookie_header` 的旧脚本行为不变。
>
> Authorization 的来源：Chrome 插件观察浏览器实际请求头中的 Authorization
> （范围内主机），与 Cookie 同一推送链路、同一接收页面展示。

## 2. 生成的固化脚本的编写约定

1. **执行环境**：脚本由本项目的 Python 虚拟环境执行（`.venv`），
   运行方式为 `python -u 脚本路径`，**不传递任何参数**。
2. **依赖**：优先使用 `requests`（已随工具安装在虚拟环境中）；
   如需其他虚拟环境中不存在的第三方库，提醒人工将其加入
   `chrome_capture_operate_python/requirements.txt` 并重新运行
   `install.bat` 安装。
3. **输出**：脚本需要在 stdout 打印必要的信息（执行进度、关键结果、错误原因），
   Web 页面读取 stdout/stderr 展示。
4. **HTTPS 证书**：发送 HTTPS 请求时**不要进行证书等任何验证**
   （`requests` 传 `verify=False`），以兼容内部系统的自签/内网证书。

## 3. 脚本示例骨架

```python
import sys
import requests

COOKIE_API = "http://127.0.0.1:33445/api/cookies/query"
TARGET = "https://www.example.com/api/data"

def get_auth_headers(url):
    """取 Cookie 与 Authorization 请求头（其一为空即不带）。"""
    try:
        r = requests.get(COOKIE_API, params={"url": url}, timeout=10)
    except requests.RequestException as e:
        print("Cookie 服务不可达（chrome_capture_operate 未启动？）:", e)
        sys.exit(1)
    if r.status_code != 200:
        print("获取 Cookie 失败:", r.json().get("error"))
        sys.exit(1)
    d = r.json()
    headers = {}
    if d.get("cookie_header"):
        headers["Cookie"] = d["cookie_header"]
    if d.get("authorization"):
        headers["Authorization"] = d["authorization"]
    return headers

def main():
    resp = requests.get(TARGET, headers=get_auth_headers(TARGET),
                        timeout=30, verify=False)
    print("状态码:", resp.status_code)
    print(resp.text[:2000])

if __name__ == "__main__":
    main()
```
