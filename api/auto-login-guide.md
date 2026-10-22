# 自动登录脚本实现要求（auto-login-guide.md）

本文档面向**生成自动登录 Python 脚本的 AI（或人）**：脚本用于在**用于抓包的
Chrome**中判断目标网站是否已登录，未登录时通过主应用提供的 HTTP 接口操作页面
完成登录。生成的脚本**不直接使用 Playwright/CDP**，只依赖 `requests`；账号值
也只在主应用中使用，不进入生成脚本。

服务默认 `http://127.0.0.1:33445`；抓包 Chrome 的调试端口以参数配置页实际值
为准（默认 9222），由主应用内部连接。

## 1. 环境与依赖

- 脚本在项目虚拟环境中执行（`python -u`），只需 `requests`（标准库以外）；
- **不要 `import playwright`，不要手写 CDP/WebSocket**；页面操作统一调用
  主应用的 `POST /api/browser/actions`；
- 脚本启动时先调用 `POST http://127.0.0.1:33445/api/chrome/start` 确保抓包
  Chrome 已启动；已启动时该接口直接返回未重复启动，`ok=false` 时打印明确
  错误并退出；
- 脚本不负责抓包开关；接口不可达时打印明确错误并退出；
- 脚本开头按项目约定把 stdout/stderr 重配置为 UTF-8（Windows 直接命令行
  运行避免中文乱码），参考 `api/api.md` 第 4 节骨架。

启动抓包 Chrome 的参考实现：

```python
def ensure_cdp_chrome():
    """确保抓包 Chrome 已启动；已启动时主应用不会重复启动。"""
    try:
        r = requests.post(SERVICE + "/api/chrome/start", timeout=30)
    except requests.RequestException as e:
        print("启动抓包 Chrome 接口不可达（服务是否运行？）：%s" % e)
        return False
    try:
        data = r.json()
    except ValueError:
        print("启动抓包 Chrome 接口返回非 JSON 内容")
        return False
    if not data.get("ok"):
        print("启动抓包 Chrome 失败：%s" % (data.get("message") or "未知错误"))
        return False
    print(data.get("message") or "抓包 Chrome 已就绪")
    return True
```

在 `main()` 最开始调用；返回 `False` 时直接退出，不要继续读取 Cookie。

## 2. 账号参数（全部由主应用解析）

登录网址与账号信息保存在全局配置文件：

```
C:\Users\%username%\.chrome_capture_operate\global_conf.json
```

- `login_url`：登录网址（d 标签页文本框输入后写入全局配置）；
- `login_accounts`：**纯扁平 Map**，key 为参数名称（如 `test_username`），
  整体使用，不拆分、不解析、不设置账号标识层；value 为实际参数值。

```json
"login_url": "https://example.com/login",
"login_accounts": {
  "test_username": "登录用户名",
  "test_password": "登录密码",
  "test_main_account": "主账号（多字段登录时使用）"
}
```

提示词中的“登录账号等参数映射”给出**页面输入字段描述 -> 参数名称**，例如：

```text
- 用户名的值对应全局配置文件账号信息中的 test_username 参数值
- 密码的值对应全局配置文件账号信息中的 test_password 参数值
```

要求：

- 生成脚本只保存**参数名称**，不读取、不保存、不打印具体账号值；
- 填写账号字段时使用动作的 `value_from` 指定参数名称，由主应用从
  `login_accounts` 取值并填入页面；
- 参数名称不存在时，`/api/browser/actions` 返回明确错误，脚本应打印并退出；
- 密码类字段只由主应用填入页面，脚本输出不得出现具体值。

> `/api/login_accounts/params` 仍可用于其他需要取值的脚本；自动登录脚本
> 不需要调用它，使用 `value_from` 即可。

## 3. 登录态判断（脚本主流程第一步）

按以下顺序判断，**已登录则跳过登录并成功退出**：

1. **先确保抓包 Chrome 已启动**：
   `POST http://127.0.0.1:33445/api/chrome/start`
   → `{"ok": true/false, "message": "..."}`；`ok=false` 时报错退出。
2. **只使用抓包 Chrome 的 Cookie 接口**：
   `GET http://127.0.0.1:33445/api/cookies/cdp?url=<登录网址>`
   → `cookie_header` / `authorization`（服务需在运行）。
   **禁止使用 `/api/cookies/query`（日常 Chrome 快照），也禁止“日常 Chrome
   查不到再回退 CDP”**——自动登录判断的是抓包 Chrome 是否已登录；日常
   Chrome 已登录而抓包 Chrome 未登录时，回退会误判为已登录并跳过登录。
3. 由 AI 选择一个**合适的接口**用于判断（生成脚本时从抓包记录或页面/网络
   分析中确定）：
   - 优先 GET、只读、响应小、**明确需要登录才能访问**；
   - 把选定的接口与判断依据写入脚本 README，便于人工校正。
4. 用 `requests` 携带 `Cookie`（如有 `authorization` 也带上）调用该接口，
   判断口径：

   | 返回 | 判定 |
   |---|---|
   | 2xx 且业务数据正常（非登录页 HTML） | 已登录 |
   | 401 / 403 | 未登录 |
   | 3xx 重定向到登录页 / 200 但返回登录页 HTML | 未登录 |
   | 网络错误 / 服务未启动 | 报错退出（不盲目执行登录） |

   `cookie_header` 为空时可直接判定未登录（跳过接口调用）。
5. 页面判据作为兜底：页面出现登录表单（如密码输入框）或 URL 含
   login/signin/passport 等特征 → 未登录。
6. 未登录 → 执行第 5 节页面登录；**登录成功后重新取一次 Cookie 调同一
   接口复验**，通过才报告成功。

### 3.1 如何确定登录态判断接口（脚本生成阶段）

仅凭 Cookie 存在不能证明登录有效，必须优先调用一个**需要登录才能访问**的
网站接口来确认。生成脚本时按以下方式确定接口：

1. **查看抓包记录**：从已有会话目录的请求中找到登录后才出现的接口；
2. **使用 chrome-devtools-attach MCP** 操作/分析抓包 Chrome 的页面与网络，
   观察登录前后的请求差异；
3. **使用 chrome-operate MCP** 在需要时开启/结束抓包，并获取抓包会话目录、
   索引与记录；常用工具：`start_capture`、`stop_capture`、
   `get_capture_status`、`list_capture_sessions`、`run_script`；
4. 若抓包 Chrome 当前已登录，可选择登录后业务请求中的轻量 GET 接口；
   若未登录且没有可用的登录后抓包，**向人工确认**（请人工登录一次或提供
   抓包记录），不要猜；
5. 选定接口后写入脚本 README（URL、方法、判断依据），便于人工校正。

**没有登录后抓包时的两阶段流程：**

1. 先生成一个仅执行登录的临时脚本（如 `login_bootstrap.py`）：只包含
   `goto/fill/click/wait/get_url/get_text`，**不做登录态判断**；
2. 通过 `chrome-operate` MCP 的 `run_script` 运行该临时脚本；账号值由
   `value_from` 在主应用内解析，AI 不接触密码；
3. 确认页面已离开登录页后，再使用 `chrome-devtools-attach` 或
   `chrome-operate` 抓包/分析登录后请求，选定登录态判断接口；
4. 生成正式 `login.py`（含登录态判断），最后验证：
   - 已登录时运行应跳过登录；
   - 退出登录后运行应完成登录并复验通过。

**安全**：登录提交的请求体可能包含密码，**不要在提交登录时抓包**；可先
`pause_capture`，登录成功后再 `resume_capture`，或登录后再开始抓包。

> MCP 工具只用于**脚本生成阶段**的分析；生成的 `login.py` 不得调用 MCP，
> 只使用 `/api/cookies/cdp`、`/api/browser/actions` 等 HTTP 接口。

## 4. 浏览器操作接口

接口契约（请求/响应/动作字段）见 `api/browser-actions.md`；本节说明自动登录
场景的用法。

生成脚本通过以下接口操作抓包 Chrome：

```http
POST http://127.0.0.1:33445/api/browser/actions
Content-Type: application/json
```

请求：

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
    {"type": "wait_for_timeout", "ms": 2000},
    {"type": "get_url"},
    {"type": "get_text", "selector": "body"}
  ]
}
```

响应：

```json
{
  "ok": true,
  "url": "https://example.com/home",
  "title": "...",
  "results": [
    {"type": "get_url", "url": "https://example.com/home"},
    {"type": "get_text", "selector": "body", "text": "..."}
  ]
}
```

失败响应（HTTP 400/500）：

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

支持的动作：

| 动作 | 必填字段 | 说明 |
|---|---|---|
| `goto` | `url` | 打开/复用目标标签页并导航 |
| `fill` | `selector` + `value_from` 或 `value` | 填写输入框；账号字段用 `value_from` |
| `click` | `selector` | 点击元素（支持 Playwright 选择器，如 `button:has-text('登录')`） |
| `press` | `selector` + `key` | 按键，如 Enter |
| `wait_for_selector` | `selector` | 等待元素；可选 `state`（visible/attached/hidden/detached） |
| `wait_for_url` | `contains` 或 `not_contains` | 等待 URL 包含/不包含指定文本 |
| `wait_for_load_state` | `state` | 等待页面加载状态：load/domcontentloaded/networkidle |
| `wait_for_timeout` | `ms` | 固定等待，仅作最后兜底 |
| `get_url` | 无 | 返回当前 URL |
| `get_text` | 可选 `selector` | 返回页面/元素文本（默认 body） |
| `get_title` | 无 | 返回页面标题 |
| `is_visible` | `selector` | 返回元素是否可见 |

等待类动作都支持 `continue_on_timeout: true`：超时后不终止整批动作，
继续执行后续的 `get_url`/`get_text`，便于判断验证码或登录失败原因。

> 本节示例中的登录网址、参数名称、选择器仅作示例。生成脚本时必须使用
> 提示词中的“页面输入字段描述 -> 参数名称”映射和实际页面选择器，不要照抄
> 示例中的 `test_username` 等名称。

约定：

- `expected_host` 必填；主应用会校验目标页面主机，防止误填到其他站点；
- 标签页按 URL 的 **协议 + 主机 + 端口** 复用；没有才新开；
- 主应用内部使用 Playwright 连接抓包 Chrome，生成脚本不接触 Playwright；
- 账号值通过 `value_from` 在主应用内解析，不会返回给脚本；
- 同一时间只允许一个登录脚本操作抓包 Chrome，避免标签页互相干扰。

当前限制：只操作主页面；暂不支持 iframe、新标签页切换、文件上传/下载。
若登录流程会跳转到其他主机（SSO），当前 `expected_host` 校验会拒绝；需要
扩展允许的主机列表后再使用。

### 4.1 登录后等待（不要固定 sleep）

登录提交后不要只写 `wait_for_timeout`。生成脚本时应通过
`chrome-devtools-attach` 分析登录后的页面事件，选择以下一个或多个信号：

- URL 变化：`wait_for_url` + `not_contains`（例如不再包含 `/auth/login`）；
- 页面加载：`wait_for_load_state`（`domcontentloaded` / `load`）；
- 登录后元素：`wait_for_selector`（如用户头像、退出按钮、控制台入口）。

推荐顺序：

```json
[
  {"type": "click", "selector": "button:has-text('登录')"},
  {"type": "wait_for_url", "not_contains": "/auth/login",
   "timeout_ms": 15000, "continue_on_timeout": true},
  {"type": "wait_for_load_state", "state": "domcontentloaded",
   "timeout_ms": 15000, "continue_on_timeout": true},
  {"type": "wait_for_selector", "selector": "<登录后元素>",
   "timeout_ms": 15000, "continue_on_timeout": true},
  {"type": "get_url"},
  {"type": "get_text", "selector": "body"}
]
```

`continue_on_timeout: true` 让等待超时后仍继续读取页面，便于识别验证码或
登录失败提示；`wait_for_timeout` 仅作最后兜底，不作为主要等待方式。

## 5. 页面登录

- **定位**：优先语义定位与稳定的 `id`/`name`；避免随机 class 与深层 CSS；
- **填写**：`fill` 动作用 `value_from` 指定参数名称；主应用负责取值和填入；
- **提交**：`click` 登录按钮或 `press` 回车；
- **等待**：优先用 `wait_for_url` / `wait_for_selector` /
  `wait_for_load_state` 等待页面变化，不要写死过长 sleep；固定
  `wait_for_timeout` 只作最后兜底；
- **成功判据**：URL 跳转离开登录页 / 出现登录后关键元素；结合第 3 节接口复验；
- **失败判据**：页面出现错误提示（如“用户名或密码错误”）、仍停留在登录页且
  无跳转；超时后报告失败并输出当前 URL（不要误报成功）；
- **验证码/短信/扫码**：stdout 提示“请在抓包 Chrome 中人工完成验证”，等待
  人工完成后继续；无法人工介入的场景在 README 注明（此类脚本不适合无人
  值守的定时执行）。

### 多字段登录示例（火山引擎 IAM 用户登录）

页面 `https://signin.volcengine.com/auth/login/user` 有三个输入框：

| 页面输入框（页面输入字段描述） | 选择器 | 参数名称 |
|---|---|---|
| 主账号名或账号ID | `#AccountInfo_input` | `test_main_account` |
| 子用户名 | `#UserIdentity_input` | `test_username` |
| 密码 | `#UserPassword_input` | `test_password` |

对应动作：

```json
[
  {"type": "goto", "url": "https://signin.volcengine.com/auth/login/user"},
  {"type": "fill", "selector": "#AccountInfo_input", "value_from": "test_main_account"},
  {"type": "fill", "selector": "#UserIdentity_input", "value_from": "test_username"},
  {"type": "fill", "selector": "#UserPassword_input", "value_from": "test_password"},
  {"type": "click", "selector": "button:has-text('登录')"},
  {"type": "wait_for_timeout", "ms": 2000},
  {"type": "get_url"},
  {"type": "get_text", "selector": "body"}
]
```

其它站点按实际页面结构映射；页面输入字段描述直接使用页面输入框的说明文本，
映射关系写入脚本 README，便于人工维护。**示例中的参数名称/选择器仅作示例，
必须以提示词映射和实际页面为准。**

当前示例脚本 `python_scripts/volcengine_login/login_and_mask_variables.py` 使用
`volc_main_acct` / `volc_sub_username` / `volc_pwd`（登录后还执行变量掩码检查）；
如果 `login_accounts` 使用其他参数名称，需修改 `FIELD_KEYS`，或按提示词重新
生成脚本。

## 6. 安全要求

1. 账号值只允许在主应用内使用：生成脚本、README、日志、异常信息、HTTP
   响应中都不得出现具体值；
2. 动作只通过 `value_from` 引用参数名称；脚本不得读取全局配置文件；
3. 只允许在 `expected_host` 匹配的页面执行填写/点击；
4. 主应用不记录动作中的 `value`；FastAPI 默认访问日志也不记录请求体；
5. `get_text` 返回的页面文本可能包含用户名等账号信息，脚本不要原样打印；
   只用于关键词判断，或脱敏后输出；
6. 不实现验证码/短信/2FA 绕过，只做人工介入提示；
7. 全局配置文件中的密码为明文（用户目录），不要把该文件放入 Git/云同步目录。

## 7. 输出与验证

- 生成的目录包含：
  - `login.py`：脚本本体（只依赖 `requests`）；
  - `README.md`：登录网址、**启动抓包 Chrome 的接口**、**用于判断登录态的
    接口**、运行方式、验证码处理说明、登录账号等参数映射（页面输入字段
    描述 -> 参数名称）、浏览器操作接口。
- 运行结束打印：成功/失败、当前 URL、耗时；失败给出可读原因。
- 生成后通过“快速执行脚本”运行一次验证；可交叉确认：
  `GET /api/cookies/cdp?url=<登录网址>` 返回登录态 Cookie（只用 CDP 接口）。

## 8. Playwright 说明（主应用内部）

- 生成脚本**不需要** `import playwright`，也不需要 `playwright install`；
- 主应用已依赖 `playwright`，通过 `connect_over_cdp` 连接已运行的抓包 Chrome，
  不启动浏览器、不下载浏览器；
- 若连接被 Chrome 拒绝（Chrome 111+ 对带 Origin 的调试 WebSocket 返回 403），
  在抓包 Chrome 启动参数中加入 `--remote-allow-origins=*`（服务侧调整）；
- 安装体积/耗时、为何不需要 `playwright install chromium`、`browser.close()`
  只断连不关浏览器等实测结论，见 `docs/Playwright安装与CDP关系实测/README.md`。