# 无配置快速使用

适用版本：0.6.1。面向Windows用户，不需要先编辑TOML或环境变量。

## 1. 双击启动

- `start-web.bat`：启动本地Web UI，服务就绪后打开浏览器。
- `start-cli.bat`：打开交互式命令行菜单。

需要Python3.11+。脚本会定位项目目录，复用或创建.venv，缺少运行依赖时安装本项目的agent依赖。首次安装可能需要网络；不会自动删除坏掉的虚拟环境、停止其他进程或调用模型。

将启动窗口显示的会话 `token` 粘贴到 Web UI 登录框，也可以从 `token_file` 指向的文件读取。服务商 API Key 填在服务商设置中，保存后会在下次启动时恢复。保持启动窗口运行即可继续使用 Agent。

高级启动参数：

```text
start-web.bat --port 8767
start-web.bat --max-history 100
start-web.bat --data-dir "D:\Model Scout Data"
start-web.bat --local-data
start-web.bat --no-browser
start-cli.bat --data-dir "D:\Model Scout Data"
start-cli.bat --local-data
```

两个入口的数据目录相同即可共用服务商记录。默认不需要指定目录。自定义目录应限制Windows ACL，避免其他用户读取会话Token；DPAPI不能代替Token文件的访问控制。端口被占用时不终止未知程序，可选择另一个端口。脚本错误时保留窗口；自动测试可设置MODEL_SCOUT_NO_PAUSE=1。

## 2. Web UI：输入到测试

1. 点击「新增服务商」，填写名称，启用 OpenAI Chat、OpenAI Responses 或 Anthropic Messages，并为所选入口填写 Base URL 和 API Key。
2. 可选填写HTTP、HTTPS、SOCKS5/SOCKS5h代理URL，支持用户名和密码。特殊字符按URL规则编码。
3. 点击“保存到本机”：成功后Key输入框清空，数据加密保存，下次启动恢复。
4. 选择服务商和入口，点击“一键获取模型”。这是列表发现，不代表模型可用。
5. 在候选列表中筛选、全选或勾选，点击“测试所选模型”；也可“测试全部（重新发现）”。
6. 在结果表查看 HTTP 状态、可用状态和诊断，可下载 JSON、XLSX（全部）或 XLSX（仅通过）。

列表受页数、响应大小和请求预算限制，返回 `partial` 时可查看原因。候选区最多同时渲染 1000 行，全选针对当前搜索和协议筛选匹配的已获取模型。过大的所选请求需要分批。托管入口默认 `chat-basic`、256 Token，服务商默认 10000 次请求预算和 10000 目标上限。Web 手动文本输入仍受 API 请求体和列表长度限制。

编辑时Key留空保留旧Key；代理留空保留旧代理，勾选“清除已有代理”才移除。存在本Agent活动任务时不可编辑/删除对应服务商。修订号防止覆盖其他窗口刚保存的修改。

## 3. 自动本机保存

Windows默认位置：`%LOCALAPPDATA%\ModelScout\providers.sqlite3`。完整服务商内容先用当前Windows用户DPAPI加密，再写SQLite，包括Key和代理密码；数据库中不写明文连接配置。SQLite事务与修订号保护并发更新。

CLI和Web读取同一存储。删除记录不等于磁盘安全擦除；加密也不等于保护已被入侵的系统账号。跨账号/系统迁移请妥善使用明确导出的配置，不假定加密数据库能直接解锁。

macOS/Linux需要安装secure-store可选依赖及可用的OS Keychain/Secret Service/KWallet；不允许纯文本keyring降级。相关原生密钥库尚未实际验收。旧`agent --config ...`保持兼容的只读注册表模式；省略config进入自动保存模式，显式data-dir也可启用保存。

## 4. 两种导出，明确区分

- **测试报告**提供脱敏结果。Web 支持 JSON 和 XLSX，命令行支持 JSON 和 CSV。
- **服务商配置**：点击“导出配置”，默认脱敏；勾选“包含API Key和代理密码”，阅读提示并确认后下载完整明文配置，文件名带.private.json。

导出的是当前连接配置，不保证与旧历史任务创建时的配置相同。完整配置包含名称、协议、Base URL、Key和代理；不包含Agent会话Token。当前没有自动导入功能。

不要上传含Key文件。浏览器下载路径由浏览器控制；CLI默认导出到exports。`.gitignore`不能保护被强制添加、已跟踪或手工上传的文件。

## 5. CLI菜单

双击start-cli.bat后，可新增、选择、编辑服务商，获取全部模型、测试全部或指定模型、导出报告/配置、删除记录。Key和代理输入不回显；编辑时回车保留，代理输入`-`清除。导出含Key配置需要明确确认，已有文件不覆盖。

流式、结构化和工具调用测试均可沿用已保存的连接，分别选择测试全部或指定模型。结构化测试验证完整 JSON，工具调用测试验证合成工具的名称和参数。

也保留原命令行参数用法，新增--api-key直接输入，但它可能进入shell历史/进程参数，因此优先交互输入或--api-key-env。HTTP/HTTPS代理与SOCKS5均为显式选择，不自动回退直连。

## 6. 验证与上传

已通过Windows DPAPI实际保存/重启恢复、API管理/修订冲突/显式导出、原生浏览器无配置流程（包括HTTP代理）及两个BAT入口测试。测试使用合成凭据，不是你的真实Key；没有预置或自动保存聊天中的Key。

.gitignore已分组注释：Python缓存/构建、运行状态、数据库、导出报告、本地凭据配置、浏览器产物。完整源码包包含BAT及bootstrap脚本；wheel用于Python安装，不包含项目根目录BAT。不要上传本机数据库、.scout、exports、个人工具配置。跨平台CI实际执行仍待环境支持。
