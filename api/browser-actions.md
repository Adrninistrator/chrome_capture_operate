# 浏览器动作接口（/api/browser/actions）

本文档只说明主应用提供的通用浏览器动作 HTTP 接口，主要供**生成的自动登录
脚本**调用。自动登录脚本生成流程、MCP 使用、登录态判断接口选择、两阶段
登录流程等见 `api/auto-login-guide.md`。

生成脚本**不直接使用 Playwright/CDP**，只通过本接口操作已运行的抓包 Chrome。
执行动作前应先调用 `POST http://127.0.0.1:33445/api/chrome/start` 确保抓包
Chrome 已启动（已启动时不会重复启动，`ok=false` 时报错退出）。

## 1. 请求

```http
POST http://127.0.0.1:33445/api/browser/actions
Content-Type: application/json
```

```json
{
  "url": "https://example.com/login",
  "expected_host": "example.com",
  "timeout_ms": 60000,
  "actions": [
    {"type": "goto", "url": "https://example.com/login"},
    {"type": "fill", "selector": "#username", "value_from": "test_username"},
    {"type": "fill", "selector": "#password", "value_from": "test_password"},
    {"type": "click", "selector": "button[type=submit]"},
    {"type": "wait_for_url", "not_contains": "/auth/login",
     "timeout_ms": 15000, "continue_on_timeout": true},
    {"type": "wait_for_load_state", "state": "domcontentloaded",
     "timeout_ms": 15000, "continue_on_timeout": true},
    {"type": "get_url"},
    {"type": "get_text", "selector": "body"}
  ]
}
```

| 字段 | 说明 |
|---|---|
| `url` | 目标登录页；主应用按协议 + 主机 + 端口复用已有标签页，没有才新开 |
| `expected_host` | 必填；主应用校验当前页面主机，防止误填到其他站点 |
| `timeout_ms` | 整批动作总超时，默认 60000，最大 300000 |
| `actions` | 按顺序执行的动作列表，最多 100 个 |

## 2. 动作

| 动作 | 必填字段 | 说明 |
|---|---|---|
| `goto` | `url` | 打开/复用目标标签页并导航 |
| `fill` | `selector` + `value_from` 或 `value` | 填写输入框；账号字段用 `value_from` |
| `click` | `selector` | 点击元素（支持 Playwright 选择器，如 `button:has-text('登录')`） |
| `press` | `selector` + `key` | 按键，如 Enter |
| `wait_for_selector` | `selector` | 等待元素；可选 `state`：visible/attached/hidden/detached |
| `wait_for_url` | `contains` 或 `not_contains` | 等待 URL 包含/不包含指定文本 |
| `wait_for_load_state` | `state` | 等待页面加载状态：load/domcontentloaded/networkidle |
| `wait_for_timeout` | `ms` | 固定等待，仅作最后兜底 |
| `get_url` | 无 | 返回当前 URL |
| `get_text` | 可选 `selector` | 返回页面/元素文本（默认 body） |
| `get_title` | 无 | 返回页面标题 |
| `is_visible` | `selector` | 返回元素是否可见 |

等待类动作支持：

- `timeout_ms`：单个动作超时；
- `continue_on_timeout: true`：超时后不终止整批动作，继续执行后续
  `get_url`/`get_text`，便于识别验证码或登录失败提示。

登录后不要只写固定 `wait_for_timeout`；优先使用 `wait_for_url`、
`wait_for_selector`、`wait_for_load_state`。如何确定登录后 URL/元素见
`api/auto-login-guide.md` 第 4.1 节。

## 3. 账号值 `value_from`

`fill` 动作使用 `value_from` 指定全局配置 `login_accounts` 的参数名称：

```json
{"type": "fill", "selector": "#password", "value_from": "test_password"}
```

- 主应用从 `login_accounts` 解析实际值并填入页面；
- 账号值不进入生成的脚本、不出现在请求/响应中、不记录到日志；
- 参数名称不存在时返回 400，`error` 含具体参数名称；
- `value` 与 `value_from` 不能同时指定。

## 4. 响应

成功：

```json
{
  "ok": true,
  "url": "https://example.com/home",
  "title": "...",
  "results": [
    {"type": "wait_for_url", "url": "https://example.com/home"},
    {"type": "wait_for_load_state", "state": "domcontentloaded"},
    {"type": "get_url", "url": "https://example.com/home"},
    {"type": "get_text", "selector": "body", "text": "..."}
  ]
}
```

失败：

```json
{
  "ok": false,
  "error": "动作执行失败（click）",
  "action_index": 3,
  "action_type": "click",
  "url": "https://example.com/login",
  "results": []
}
```

- `action_index` 从 0 开始；
- `url` 为失败时页面地址；
- `results` 为失败前已产生的结果；
- 账号值不会出现在响应中；`get_text` 可能包含页面显示的用户名等文本，
  脚本不要原样打印。

## 5. 安全与限制

- 服务仅监听 `127.0.0.1`；
- 主应用不记录动作中的 `value`，FastAPI 默认访问日志不记录请求体；
- 同一时间只允许一个登录脚本操作抓包 Chrome，避免标签页互相干扰；
- 当前只操作主页面，暂不支持 iframe、新标签页切换、文件上传/下载；
- 若登录流程跳转到其他主机（SSO），当前 `expected_host` 校验会拒绝，
  需要扩展允许的主机列表。

## 6. 相关文档

- `api/auto-login-guide.md`：自动登录脚本生成要求、MCP 使用、登录态判断、
  两阶段流程、安全要求；
- `api/api.md`：通用脚本的 Cookie/Authorization 获取与编写约定。