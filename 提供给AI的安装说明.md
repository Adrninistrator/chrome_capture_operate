# 提供给AI的安装说明

按本文档完成 chrome_capture_operate（浏览器自动化——AI 生成网页操作脚本）从软件下载、项目安装到验证的全部安装步骤。本文档面向执行安装的 AI：用户已将本文件路径提供给你，按顺序执行，每步均给出执行命令与成功判定；需要人工介入的环节会明确提示。

适用环境：Windows 10、Windows 11。

# 安装模式

先向用户确认需要哪种安装模式，再按模式裁剪安装项，避免安装不需要的内容：

|安装模式|需要的安装项|
|---|---|
|执行脚本模式|公共依赖（Chrome、Python）+ 当前项目 + Chrome插件 + 插件配置|
|生成脚本+执行脚本模式（人工操作生成脚本）|同执行脚本模式（生成脚本无额外依赖）|
|生成脚本+执行脚本模式（AI自主操作生成脚本）|同执行脚本模式 + nodejs + chrome-devtools-mcp + AI Agent的MCP配置|

# 环境检查

安装软件前先检测已安装项，已满足要求的直接跳过对应安装：

|软件|检测命令|判定标准|
|---|---|---|
|Chrome|`reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve`|输出包含 chrome.exe 的完整路径且文件存在。版本需要较新（建议近两年发布的版本，可用 PowerShell `(Get-Item <chrome.exe路径>).VersionInfo.FileVersion` 查看）|
|Python|`python --version`（失败时试 `py -3 --version`）|输出 3.10 或更高版本，且 `python --version` 能直接执行（说明已在 PATH）|
|nodejs|`node -v` 与 `npm -v`|两条命令均能输出版本（仅 AI 自主操作模式需要）|
|git|`git --version`|能输出版本（决定下载当前项目的方式）|

# 联网检查

实际执行安装前，检查当前机器是否能够访问互联网，分目标分别探测（探测结果决定后续软件下载与下载当前项目使用的来源）：

```
curl -I --connect-timeout 5 https://www.python.org
curl -I --connect-timeout 5 https://gitee.com
curl -I --connect-timeout 5 https://github.com
curl -I --connect-timeout 5 https://nodejs.org
```

- 各目标分别记录可达性：探测结果仅用于判断联网状态与**软件下载**来源的选择；**当前项目的下载地址由用户指定**（见「Git访问方式」），AI 不根据可达性自行挑选项目仓库
- 全部不可达（不能访问互联网）：需要下载的软件只能提示人工安装——向用户列出需要下载的软件与下载地址，转为引导人工按本文档步骤执行并等待完成

# 软件下载

假如能够访问互联网，每个软件都提供多个下载链接，验证哪个链接下载速度最快并使用最快的链接下载：

```
curl -o /dev/null -sS -w "%{speed_download}\n" -r 0-1048575 <下载链接>
```

（按 range 方式下载前 1MB 并输出平均速度，对每个链接各测一次，取 speed_download 最大的链接。）

环境变量总则：若软件需要环境变量（如 PATH），优先在安装时使用安装程序提供的配置选项（如 Python 的 `PrependPath=1`、MSI 的自动加入 PATH）；安装程序无此选项或安装后验证未生效时，由 AI 在安装完毕后设置环境变量（如用 `setx PATH "%PATH%;<安装目录>"` 追加，注意在**新的命令行会话**验证生效）。

各软件的下载链接与安装方式（按安装模式确定需要哪些：Chrome、Python 必装，nodejs 仅 AI 自主操作模式需要）：

## Chrome

- 官方下载页：https://www.google.com/intl/zh-CN/chrome/
- Chrome 企业版离线安装包（MSI，支持静默安装）：https://chromeenterprise.google/intl/zh-CN/download-chrome/ 下的 64 位 MSI
- **64 位企业版 MSI 官方直链（2026-09-28 实测国内可直接下载，约 4MB/s，约 160MB，始终为最新稳定版，文件持续更新）**：https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi
  （说明：Google 下载页面在国内通常不可达，但 `dl.google.com` 直链经谷歌中国 CDN 国内可达且速度远高于其他镜像——实测下载页面不可达时此直链仍可用，优先使用；可选建议：如需进一步确认文件有效性，可校验文件头为 MSI 魔数 `D0 CF 11 E0`，非必需步骤）
- 直链与页面均不可达时，提示人工从可信渠道获取 Chrome 安装包（如由可访问外网的机器下载后拷贝到本机），AI 等待人工完成
- 静默安装（MSI）：`msiexec /i <MSI文件> /qn`（需要管理员权限，可能弹出 UAC 确认框需人工点击）
- 安装后验证：chrome.exe 存在于 `C:\Program Files\Google\Chrome\Application\` 或 `C:\Program Files (x86)\Google\Chrome\Application\`

## Python

- 官方下载页：https://www.python.org/downloads/ （各版本安装包在 https://www.python.org/ftp/python/ 目录下，如 `python-3.12.10-amd64.exe`）
- 国内镜像（同版本目录）：https://mirrors.huaweicloud.com/python/
- **速度提示（2026-09-28 实测）**：官方源约 110KB/s（12MB 安装包约 2 分钟），华为云镜像约 1MB/s（快约 9 倍）——国内环境建议直接使用华为云镜像（如 `https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe`）
- 选择 3.10 或更高版本的 64 位（amd64）安装包
- 静默安装并加入 PATH：`python-<版本>-amd64.exe /quiet InstallAllUsers=0 PrependPath=1`（InstallAllUsers=0 按当前用户安装无需管理员；PrependPath=1 即安装时勾选"加入 PATH"的等价选项）
- 注意：安装完成后 PATH 在**新的命令行会话**中才生效——后续命令请在新的 shell 中执行
- 安装后验证（新会话）：`python --version` 输出 3.10 或更高版本

## nodejs（仅 AI 自主操作模式）

- 官方下载页：https://nodejs.org/ （选择 LTS 版本的 64 位 MSI，如 `node-v22.x.x-x64.msi`）
- 国内镜像（同版本目录）：https://mirrors.huaweicloud.com/nodejs/ 或 https://npmmirror.com/mirrors/node/
- **速度提示（2026-09-28 实测）**：华为云镜像约 1.15MB/s 最快，官方 nodejs.org 约 0.6MB/s，npmmirror 约 0.6MB/s——国内环境建议优先华为云镜像（如 `https://mirrors.huaweicloud.com/nodejs/v22.14.0/node-v22.14.0-x64.msi`）
- 静默安装：`msiexec /i node-v<版本>-x64.msi /qn`（MSI 安装会自动加入 PATH；需要管理员权限）
- 安装后验证（新会话）：`node -v` 与 `npm -v` 均能输出版本

# Git访问方式

下载当前项目 chrome_capture_operate——**仓库下载地址由用户指定，AI 不要自作主张选择**：

- 开始下载前先向用户确认仓库下载地址（gitee / github / 其他来源）：用户已在任务中给出地址时直接使用；未给出时向用户询问后再执行，不得自行假定或挑选
- git 可用且用户指定的仓库可达时使用 git clone：

```
git clone <用户指定的仓库地址>
```

- 没有 git 或指定仓库不可达时，通过 HTTP 接口下载压缩包并解压（地址同样以用户指定为准，参考形式：gitee 仓库的 `…/repository/archive/master.zip`、github 仓库的 `…/archive/refs/heads/master.zip`）：

```
<用户指定仓库对应的压缩包下载地址>
```

解压：PowerShell 执行 `Expand-Archive -Path master.zip -DestinationPath <目标目录>`；解压出的目录名可能带 `-master` 后缀，重命名为 `chrome_capture_operate`

# 源地址

执行 install.bat 安装 Python 依赖之前，读取项目根目录的「安装源地址.md」文件：

- 文件中写有 pip 源、npm 源的配置命令（如清华 pypi 镜像、npmmirror 镜像）；若命令非空，先执行对应命令再安装依赖（pip 源配置对 install.bat 内的依赖安装生效；npm 源配置在后续安装 chrome-devtools-mcp 时生效）
- 文件不存在或命令为空时，使用官方源

# 安装步骤

## 安装并启动Python服务

1. 进入 `chrome_capture_operate_python` 目录
2. 首次安装：运行 `install.bat`（创建运行环境并安装依赖，只需执行一次）
3. 启动服务：运行 `start.bat`——启动后系统托盘出现图标，服务自动用 Chrome 打开 Web 管理页面（默认 `http://127.0.0.1:33445`，仅本机）
4. 验证：`curl http://127.0.0.1:33445/health` 返回 `{"ok": true, ...}`

## 安装Chrome插件

优先由 AI 调用程序接口完成（需 Python 服务已启动；接口失败或前提不满足时，转为提示用户按 Web 页面页头**安装Chrome插件-人工**按钮中的人工安装步骤操作）：

- 方式一（推荐，模拟点击安装，约 10 秒）：

```
curl -X POST http://127.0.0.1:33445/api/extension/ui_install
```

前提：日常使用的 Chrome 已运行且界面语言为中文；执行期间提示用户不要操作鼠标键盘

- 方式二（注册表安装）：

```
curl -X POST http://127.0.0.1:33445/api/extension/registry_install
```

需要人工点击 UAC 确认框；安装后无法通过 Chrome 扩展程序页面卸载，卸载需调用 `POST /api/extension/registry_uninstall`

- 安装进度查询：

```
curl http://127.0.0.1:33445/api/extension/install_status
```

（running 为执行中，done 为完成，error/日志字段含失败原因）

## Chrome插件配置（必须）

不配置的影响：插件默认"全部禁止"，不会向 Python 服务推送任何 Cookie，所有脚本执行时返回未登录/401。必须改为"全部允许"或"按指定范围推送"：

- 读取当前配置：

```
curl "http://127.0.0.1:33445/api/extension/config"
```

- 修改为全部允许（接口会自动回读验证，返回 ok 即生效）：

```
curl -X POST http://127.0.0.1:33445/api/extension/config -H "Content-Type: application/json" -d "{\"push_scope\": \"all\"}"
```

- 或按清单推送（allow_list 支持通配符，注意需带 `*.` 才能匹配子域）：

```
curl -X POST http://127.0.0.1:33445/api/extension/config -H "Content-Type: application/json" -d "{\"push_scope\": \"list\", \"allow_list\": [\"*.example.com\"]}"
```

- 接口不可用时，引导用户点击 Web 页面页头**Chrome插件设置**按钮打开插件页面人工配置

## 生成脚本-AI自主操作（仅该模式需要）

1. 安装 nodejs（见"软件下载"）
2. 安装 chrome-devtools-mcp（nodejs 项目，全局安装）：

```
npm install -g chrome-devtools-mcp
```

3. MCP 服务不在 AI Agent 中安装：chrome-devtools-mcp 对应的 nodejs 项目已在上一步安装，chrome-operate 的 MCP 服务由已启动的 Python 服务提供（SSE），chrome-devtools-attach 亦由 chrome-devtools-mcp 包提供——本安装流程不执行 `claude mcp add` 等 MCP 注册配置命令。需要使用 AI 自主操作时，由用户在所用 AI Agent 中自行配置 MCP（可在 Web 页面页头**AI自主操作Chrome**弹窗中输入项目目录后点击**配置**按钮一键完成）

# 验证

全部安装完成后的验证（每项给出成功判定）：

1. **Python服务**：`curl http://127.0.0.1:33445/health` 返回 `"ok": true`（端口以参数配置实际值为准）
2. **Chrome插件已推送Cookie**：提示用户在日常使用的 Chrome 登录需要操作的网站，在插件**参数配置**标签页点击**立即推送一次**（或等待自动推送），然后 `curl http://127.0.0.1:33445/api/cookies/receives` 检查最近推送记录（数量大于 0 且成功）；也可在 Web 页面**Chrome Cookie接收状态**标签页查看
3. **整条链路**：执行示例脚本验证从安装到执行的完整链路：

```
curl -X POST http://127.0.0.1:33445/api/scripts/run -H "Content-Type: application/json" -d "{\"path\": \"<项目根目录>/chrome_capture_operate_python/python_scripts_example/health_check/main.py\"}"
```

（返回 `{"exec_id": "<id>"}`；路径分隔符可用 `/`。之后轮询）

```
curl http://127.0.0.1:33445/api/exec/<id>
```

成功判定：`running` 为 false 且 `exit_code` 为 0（与在 Web 页面**快速执行脚本**标签页执行 health_check 等效）

验证全部通过后，安装完成。生成的脚本可按 README.md「使用说明」章执行：Web 页面快速执行、定时执行、系统托盘执行，或由 AI Agent 执行。
