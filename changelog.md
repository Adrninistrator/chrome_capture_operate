# 20261109

- 快速执行脚本页支持脚本运行参数（可选，脚本需要时使用）：`foo.py` 同目录 `foo.ini`（`[params]` + `# 中文说明` + `key = value`）；执行区新增「运行参数」按钮，弹窗用「参数展示方式」单选框切换表单（逐项编辑中文说明/参数名/值）与文本（直接编辑 INI）视图，保存前校验语法
- 新增 `GET/PUT /api/scripts/ini`：只允许脚本根目录内同名 `.ini`，INI 语法校验、256KB 上限、UTF-8/LF 写入
- 生成脚本提示词补充：运行参数不是必须的；需要时生成同名 `.ini`，脚本用 `configparser` 读取、缺省用内置默认值
- 快速执行脚本文件列表展示 `.ini` / `.ini.example`：点击 `.ini` 直接编辑运行参数（按后缀判断，不要求同名 py，支持多脚本共用配置），`.ini.example` 只读查看
- 运行参数文件格式不符合「表单」视图时，页面提示具体原因并保持文本视图可编辑；保存校验失败只提示，不抛异常
- 新增 `POST /api/chrome/open_url`：在日常使用的 Chrome 打开 URL；`profile` 默认 `0`（默认 profile），非 `0` 为 Chrome 多开实例编号；仅允许 http/https，`api/api.md` 补充说明

# 20261107

- 快速执行脚本页面点击「执行」后立即禁用「执行/执行多次」按钮，执行结束或启动失败后恢复；覆盖按钮点击到 `/api/scripts/run` 返回之间的窗口期，避免同一脚本被重复/并发触发
- 新增 `/api/browser/actions` 可选字段 `activate_page`（默认 `true` 保持原行为）：为 `false` 时跳过 `page.bring_to_front()`，避免每次页面登录把 CDP Chrome 窗口激活到用户前台；`BrowserActionsBody` → `run_actions` → `_find_or_open_page` 全链路透传，`api/browser-actions.md` 补充字段与限制说明
- 补充 `activate_page=false` 的前台窗口恢复兜底：登录成功跳转仍可能被 Chrome/页面 JS 激活窗口，`run_actions` 记录调用前的前台窗口，在页面创建、每个动作、长等待及整批结束时把被 Chrome 抢走的焦点还给用户（仅 Chrome 窗口时恢复、0.5 秒节流、非 Windows 不生效）；`api/browser-actions.md` 同步说明
- 后台模式消除窗口“闪一下”：`activate_page=false` 且无匹配标签页时，复用任意 http/https 标签页导航而非 `new_page()`（避免最小化窗口被 restore）；`wait_for_selector` / `wait_for_load_state` / `wait_for_timeout` 改为 500ms 分段轮询，每段之间恢复前台焦点

# 20261102

- `fill` 动作失败时返回安全的分类说明（超时/不可见/不可编辑），仍不回显可能含账号值的原始异常；`api/auto-login-guide.md`、`api/browser-actions.md` 补充必需元素等待建议
- 浏览器动作新增 `reload`（刷新当前标签页）

# 20261030

- 参数配置-企业微信消息增加醒目提示“多应用必须使用独立的机器人”（同一机器人只能保持一个长连接，多应用共用会互相踢线）；新增“企业微信机器人创建说明”按钮，弹框展示创建步骤（机器人菜单/API 模式/长连接/仅个人使用/保存/通讯录提醒）与多应用独立机器人的原因、权限说明
- 定时任务“每隔一段时间执行”支持指定星期x：新增任务/编辑时勾选周一~周日（可多选，不选=每天），并提供“全选”一键勾选/取消全部星期x；星期限制与“允许执行的时间段”同时生效；任务列表描述显示指定星期
- interval 调度点计算改为按天推进并用整数微秒求 `start+k*间隔` 边界，支持星期限制下跨天查找，避免 1 秒级间隔逐点遍历；`next_run/latest_due` 在星期与时间段范围内取最近点
- 参数配置页新增“全局配置文件”：展示完整路径（点击全选）、复制路径、下载 JSON 备份；新增 `GET /api/global_conf/file`（文件不存在返回 404），`GET /api/config` 返回 `global_conf_path`/`global_conf_exists`
- 统一启动入口为 `app.main`：start.bat 与 PyCharm 都运行同一模块，start.bat 只负责选择解释器；启动成功（`/health` 就绪）、失败、已在运行时在桌面右下角弹出自动关闭的提示（成功显示访问地址，失败提示查看 log 目录），`msgbox` 不再阻塞；`.venv` 缺失时 start.bat 用系统 Python 运行 `app.main --venv-check`，由同一模块弹出安装提示；端口已监听时用 `/health` 区分本程序（已在运行）与其他程序（端口占用）

# 20261029

- 新增企业微信消息通道（移植自 wecom-aibot/send_recv_wecom_msg）：后台 WSS 长连接订阅鉴权、心跳保活、断线重连，按消息序号接收用户回复；脚本执行到需要短信验证码/动态 Token/一次性口令时，可经企业微信通知用户并取回回复
- 参数配置页新增“企业微信消息”：BotID、长连接密钥、会话 ID、会话类型、消息保存时长、长连接地址；保存到全局配置 `wecom_config`，密钥使用 Windows DPAPI 加密（不回显、留空保持），保存后自动重启长连接；展示配置/连接状态与最近错误，支持清除配置
- 新增 HTTP 接口：`POST /api/wecom/send_message`（消息内容 + 4 位消息序号）、`GET /api/wecom/get_reply?seq=...`（只指定消息序号）、`GET /api/wecom/status`、`GET/PUT/DELETE /api/wecom/config`；回复按序号归属，序号/内容校验，发送失败回滚序号
- d. 自动登录脚本提示词页面增加“企业微信接收短信验证码/动态 Token”说明与文档路径；默认提示词不包含该功能，由用户按需补充要求
- 文档：新增 `api/wecom-message.md`（该功能只在本文档说明）；更新 README、参数配置需求（prompt-Python项目.md）
- 优化 d. 自动登录脚本提示词页面：页面支持上下滚动（底部复制按钮可达）；多账号同时登录说明、企业微信说明改为独立弹框（低频功能不占页面空间）；新增映射后自动滚动并聚焦新行，提示词立即以占位文本反映未填写的映射；提示词主体改为可编辑（带 markdown 高亮），用户直接在 `# 要求填写` 节补充要求（不再单独提供补充要求框），重新生成时保留已填内容；输出明确要求生成 `login.py`、`README.md`、`prompt.md`（本次使用的完整提示词）；提示词模板恢复 `# 参考文档` 节，引用 `api/auto-login-guide.md`、`api/browser-actions.md`、`api/api.md`，请求体/动作字段/输出文件等细节移入 api 文档，模板只保留任务参数、接口 URL、`# 要求填写` 与输出目录

# 20261028

- 新增多账号多 CDP 实例：全局配置 `cdp_chrome_profiles`（端口为唯一标识，独立调试端口与数据目录）；9222 默认抓包实例自动登记、禁止删除/改端口并继续使用旧目录；9223–9999 登录实例由自动登录脚本按需创建，使用空 profile 与 `<base>_<port>` 目录
- 新增 CDP 实例接口：`GET/POST /api/cdp-profiles`、`PUT/DELETE /api/cdp-profiles/{port}`、`POST /api/cdp-profiles/{port}/open|bring-to-front`、`POST /api/cdp-profiles/ensure`；ensure 按端口幂等创建/启动，端口被其他程序或未登记 Chrome 占用时报错且不抢占；删除只移除登记、保留 profile，同端口再次 ensure 接管旧目录
- 新增 `start_cdp_chrome(port, profile_dir)` 与 CDP 就绪等待；`/api/chrome/start` 保留为 9222 兼容入口
- `/api/browser/actions` 新增 `port`（`cdp_profile_id` 为兼容别名），只允许抓包端口或已登记端口；浏览器动作锁由全局改为按端口，同一实例串行、不同实例可并发
- `/api/cookies/cdp` 只允许抓包端口或已登记端口；非 9222 实例只返回 Cookie，不提供 Authorization（不抓包、不装插件）
- 新增“CDP实例”标签页：端口、名称、用途、CDP 状态、数据目录、是否自动创建，支持新增/改名/打开/置顶/删除；自动创建的实例可见
- d 标签页增加多账号同时登录说明（可要求 AI 或人工修改 `CDP_PORT`/`ACCOUNT_NAME`）；自动登录提示词携带 CDP 端口、ensure 与 Cookie 接口、浏览器动作 `port`
- 示例 `login.py` 支持显式 `CDP_PORT`/`ACCOUNT_NAME` 与 ensure；文档更新 README、api/auto-login-guide.md、api/browser-actions.md、api/api.md、docs/自动登录脚本/README.md

# 20261024

- 自动登录账号支持“密码类型”：管理页面可勾选密码类型，值使用 Windows DPAPI 用户级加密保存，页面/GET 接口固定显示 `********`，编辑留空保持原值，取消密码类型时解密；`value_from` 在主应用内解密使用，`/api/login_accounts/params` 不返回密码类型参数；账号值与密码类型名称同锁原子写入

# 20261022

- 新增自动登录脚本生成功能：AI自主操作Chrome 弹窗增加 d. 自动登录脚本提示词；`login_accounts` 改为纯扁平 Map（key 整体作为参数名称），提供通用账号管理页面（参数名称/参数值 CRUD、全局配置路径），d 标签页人工维护“页面输入字段描述 -> 参数名称”映射；提示词精简为任务参数、MCP 工具、启动抓包 Chrome/浏览器接口、指南路径和输出目录，登录网址或映射不完整时禁用复制
- 新增 `POST /api/browser/actions`：主应用内部使用 Playwright 连接抓包 Chrome、复用标签页，支持 goto/fill/click/press/wait_for_selector/wait_for_url/wait_for_load_state/wait_for_timeout/get_url/get_text/get_title/is_visible；fill 支持 value_from，由主应用从 login_accounts 解析，账号值不进入生成脚本；等待动作支持 continue_on_timeout，示例脚本不再固定 sleep
- 生成的登录脚本启动时调用 /api/chrome/start 确保抓包 Chrome 已启动；登录态只使用 /api/cookies/cdp；无登录后抓包时按两阶段流程（先生成 login_bootstrap.py，通过 chrome-operate 的 run_script 运行，登录成功后再分析登录态接口）；MCP 仅用于生成阶段，生成的脚本不使用 MCP/Playwright
- 文档：新增 api/auto-login-guide.md、api/browser-actions.md；浏览器动作接口从 api/api.md 拆分；提示词模板、README、prompt-生成完整文档更新（独立自动登录章节、test_ 示例、从页面复制提示词）；补充定时执行脚本无人值守要求

# 20261013

- 新增MCP工具query_cookies_cdp：AI Agent经MCP实时读取用于抓包的Chrome的Cookie与Authorization（与HTTP接口同口径，生成脚本期间验证登录态不再依赖插件推送的日常Chrome快照）

# 20261011

- 噪声检测新增量级判据：超高频（次数≥100）且响应极小（平均≤512B）的接口也判为噪音——修复真实多页面会话上成组上报的埋点与多页面交错的登录态心跳全部漏检的问题（周期判据对间隔不规律的这两类形态无效）；提示条支持"忽略全部"，各处文案区分两类判据
- 修复一键忽略后页面"忽略的URI"列表不刷新的问题（此前旧状态会在下次保存抓包配置或开始抓包时把规则冲掉）；MCP状态查询新增重复次数最多的接口统计（repeat_stats，含是否命中噪声判据标志），供AI对大响应周期刷新等未判定的大量重复请求按业务判断是否忽略
- 抓包中自动检测高频重复的轮询/心跳类请求（同接口重复多、间隔规律、返回小）：抓包页非模态提示条可一键忽略（按路径加入忽略规则，仅对后续数据生效），结束抓包时汇总提示，清单写入生成提示词模板（AI分析时忽略），MCP状态查询返回清单供AI判定；检测为建议性质，不自动忽略任何请求
- 修复暂停抓包期间前端页面操作事件仍写入时间线的问题（暂停现在对网络请求与页面操作同样生效，操作注释不受影响）
- MCP说明、生成脚本提示词模板与AI分析建议中新增引导：开始抓包前先查询访问过的域名并设置需要抓包的网址（仅目标业务域），同域的高频轮询/心跳用忽略的URI规则（前缀匹配）排除，避免埋点/心跳等无关请求混入抓包记录

# 20261010

- 修复保存参数（含开机自启动）偶发失败的问题：conf.json 被其他程序占用或被安全软件拦截时不再报错失败，参数在当前服务生效并提示重启后回退；只修改自启动时不再依赖conf.json写入
- 快速执行脚本页新增"执行多次"按钮：弹窗输入执行次数（最多1000次），依次执行并在输出区分段展示，"结束进程"会中止后续次数
- 定时执行任务新增"执行日志保留天数"（默认30天，新增/编辑时可配置，超期自动删除）
- 执行日志列表每行新增"查看"文字按钮（整行也可单击打开）
- 系统托盘快速执行脚本的独立输出窗口新增"执行"按钮：清空输出并重新执行，仅当前次执行结束后可用

# 20260930

- 修复模拟点击安装插件在部分电脑上失败的问题（导航时序竞态、文件对话框输入框选错、可能选中最小化窗口、失败残留对话框）
- 系统托盘"打开页面"与启动后自动打开统一改用Chrome打开（即使Chrome不是默认浏览器），找不到Chrome时退回默认浏览器

# 20260929

- 增加MCP功能，以支持AI自主在Chrome执行操作与抓包，并生成脚本
- 优化Chrome安装当前项目插件后，打开部分网站会卡死问题
- Chrome插件支持程序自动安装与配置
- 抓包支持页面操作自动记录（DOM注入）：操作与请求毫秒级对齐进同一条时间线；页面刷新后自动重新注入，导航窗口期延迟到文档提交后注入；记录的事件类型可在参数配置选择（默认排除噪音与重复事件）
- Cookie查询留痕改为结构化JSON日志文件（cookie_query.log），页面查询记录展示当天最新50条（从日志文件读取，服务重启不丢），页面增加日志查看说明（目录与文件名）
