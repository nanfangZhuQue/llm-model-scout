# 本地 Agent 使用教程

适用版本：0.6.1 · 更新日期：2026-10-01

Agent 是本地 HTTP 服务，供脚本与同源 Web UI 创建任务、查询进度、取消和下载报告。它把脱敏扫描历史保存到本机。服务商连接配置由独立的加密数据库保存。[Web UI 教程](web-ui.md)

## 0.6.1无配置模式

推荐双击start-web.bat。desktop/不带config的agent自动加载本机加密存储，允许在鉴权界面新增服务商；旧--config方式仍作为兼容只读注册表。具体输入、保存与含Key导出见[快速使用](quick-start.md)。以下原有任务接口保持兼容。

新增管理接口：POST /providers创建，PUT /providers/{id}携带revision更新，DELETE /providers/{id}?revision=N删除；运行中禁止本Agent修改对应服务商，修订冲突返回409。GET列表不返回Key。

POST /providers/{id}/export默认脱敏；只有include_secrets=true且confirm_plaintext=true才返回含Key附件，不接受文件路径。POST /scans支持operation=models（仅发现）及model_selections按入口选择模型；发现结果读取任务JSON报告。

托管服务商可各保存一个 `openai_chat`、`anthropic_messages` 和 `openai_responses` 入口。三种入口的 ID 分别为 `openai`、`anthropic` 和 `responses`。Responses 凭据按协议独立保存，编辑时可沿用该入口已保存的 Key。TOML 注册表继续使用各入口的显式 ID。

## 1. 安装与本地演示

在项目根目录执行（Windows；其他平台替换Python路径）：

```powershell
.venv/Scripts/python -m pip install -e ".[agent]"
.venv/Scripts/python examples/agent_demo.py
```

演示自动启动临时mock和真实Agent进程，完成鉴权、创建任务、查询、JSON及CSV下载后清理本次进程；不产生外部服务商费用。预期四个合成目标，2通过、2模拟不存在。测试环境仅在内存/进程环境中使用假凭据。

开发依赖`.[dev]`也包含Agent测试所需依赖。

## 2. 正式启动

准备`providers.local.toml`及其引用的API Key环境变量，配置格式沿用[配置参考](configuration.md)。Agent启动时加载全部Provider，每个name是唯一的字母/数字/点/下划线/连字符标识。

```powershell
.venv/Scripts/python -m llm_model_scout agent --config providers.local.toml
```

默认地址为 `http://127.0.0.1:8766`，仅绑定 IPv4 回环地址。Agent 关闭代理头信任、访问日志和公开 Swagger/OpenAPI。启动时 stdout 输出 JSON，包含 `agent_url`、`token_file` 和本地会话 `token`。使用 `--token` 或 `--token-env` 时，`token_file` 为 `null`。客户端可请求 `/health` 确认服务就绪。会话 Token 可用于访问本地 Agent，分享启动日志前应将其脱敏。

| 启动参数 | 默认值 | 说明 |
|---|---|---|
| --config | 可选 | 兼容的本地服务商注册表；省略进入自动保存模式 |
| --port | 8766 | 1—65535，绑定地址不能修改 |
| --token-file | .scout/agent.token | 随机会话token，独占新建，不覆盖已有文件 |
| --token-env | 未设置 | 引用已存在的32—256位URL安全会话token，与token-file互斥 |
| --token | 未设置 | 显式会话token（32-256 URL-safe字符），与token-env/token-file互斥 |
| --local-data | 未设置 | 使用当前工作目录下的 `data/` 保存本机数据 |
| --data-dir | 用户数据目录 | 指定扫描历史与连接配置的数据目录，优先级高于 `--local-data` |
| --max-running | 2 | 同时运行任务，CLI上限16 |
| --max-queued | 16 | 排队容量，CLI上限256 |
| --max-history | 50 | 磁盘最多保留的终态任务数，可设为 1—1000 |

文件中的token是Agent本地会话凭据，不是服务商API Key。POSIX创建文件为0600、目录为0700（新建时）；Windows须自行确保所在目录ACL仅允许可信用户访问，Python mode不等于NTFS隔离保证。

正常退出清理本次创建且内容未改变的token文件；强杀可能留下文件。先确认旧Agent已停止，再处理遗留文件，或使用新的--token-file路径。不要覆盖运行中Agent的token。使用token-env时不会创建或删除token文件。

注册表仅读取明确的凭据环境变量引用，普通SCOUT_BASE_URL等环境覆盖不会更换服务端批准的路由。凭据引用名称不得与SCOUT_*保留设置名称冲突；SCOUT_MOCK_UPSTREAM_KEY等自定义名称可以使用。

## 3. 鉴权与请求来源

GET /health只返回status与version，不需要token，但仍受Host/Origin检查。其他数据接口使用`Authorization: Bearer <Agent会话token>`，不要传服务商Key；固定UI静态资源可匿名读取但仍受Host/Origin校验。

- Host仅允许当前端口的127.0.0.1/localhost；忽略X-Forwarded-*，不接受任意域名。
- 没有Origin的本地脚本可用；有Origin时必须精确为同源http://127.0.0.1:端口或http://localhost:端口。
- 拒绝重复Host/Authorization、跨站或null Origin；没有通配CORS。
- POST /scans要求application/json；请求体最多64KiB，读取截止5秒，不支持压缩请求体。
- 参数错误只返回固定文案，不回显提交的凭据或整个请求。

在自己的PowerShell中读取会话token到变量，不打印它：

```powershell
$agent = 'http://127.0.0.1:8766'
$headers = @{ Authorization = 'Bearer ' + (Get-Content .scout/agent.token -Raw).Trim() }
Invoke-RestMethod "$agent/providers" -Headers $headers
```

## 4. 创建和查询任务

```powershell
$body = @{
  provider_id = 'vendor-a'
  endpoint_id = 'openai'
  models = @('YOUR_MODEL_ID')
  profile = 'chat-basic'
  max_tokens = 256
  max_requests = 3
  scan_timeout = 45
} | ConvertTo-Json
$job = Invoke-RestMethod "$agent/scans" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod "$agent/scans/$($job.scan_id)" -Headers $headers
```

替换为本地注册表和真实model ID后才会实际调用服务商。models省略时进行发现；提供时非空、去重，最多1000项且不超过Provider的max_models。API扫描请求不接受base_url、api_key、proxy、out或任何任意文件路径；服务商管理接口在鉴权后专门接受连接信息。未知创建字段返回422。

| 请求字段 | 说明 |
|---|---|
| provider_id | GET /providers返回的标识，保存的服务商通常为p_开头；旧TOML模式为name |
| mode | `batch` 为默认批量验收。`quick` 用于验证一个显式指定的模型 |
| endpoint_id | 已配置入口ID或all；多入口必须显式选择 |
| models | 可选字符串数组，每项1—256字符，省略沿用配置/发现 |
| model_selections | 可选的入口 ID 到非空模型列表的映射，一次最多选择 3 个入口，与 `models` 互斥 |
| profile | 可选baseline/chat-basic，省略沿用各入口配置 |
| capability | `text`、`stream`、`structured` 或 `tool`，省略沿用入口配置。不同能力的入口同批扫描时须显式指定一个值 |
| max_tokens | 可选正整数，上限65536；不自动调整 |
| max_requests | 可选正整数，不超过服务端Provider配置 |
| scan_timeout | 可选正数，不超过服务端配置与300秒的较小值 |

POST 成功返回 202 与 `scan_id`。`GET /scans/{scan_id}` 可查询保留的任务，返回 `state`、时间、`progress` 和 `report_available`。状态包括 `queued`、`running`、`cancelling`、`completed`、`failed`、`cancelled` 和 `interrupted`。只能下载检查点中的数据时，摘要含 `report_partial=true`。

流式、结构化及工具调用任务的摘要和结果行分别包含 `capability: "stream"`、`capability: "structured"` 或 `capability: "tool"`。未含该字段的文本结果属于 `text`。扫描预览返回本次选择的能力。仅发现任务使用列表接口，支持省略能力或设为 `text`。

`GET /scans?limit=50&offset=0&provider_id=vendor-a` 按创建时间从新到旧查询。省略 `provider_id` 可查看全部任务，指定时按保存的服务商 ID 精确匹配。返回 `scans`、筛选后的 `total`、`limit`、`offset` 和未受筛选影响的 `providers` 选项。分页大小为 1—100，偏移量为非负整数。服务商改名或删除后，任务详情仍显示该次扫描保存的名称。

| 进度字段 | 含义 |
|---|---|
| `discovered` | 各入口已知候选数之和，含手动候选 |
| `planned` | 过滤并应用模型上限后的验证目标数。计划形成时出现，可为 0。仅发现任务省略该字段 |
| `finished` | 已得出 `available` 或 `unavailable` 结论的目标数 |
| `skipped` / `cancelled` | 计划内未执行或在途取消的目标数，计划形成时出现 |
| `total` | 终态报告的结果行数，包含因模型上限跳过的报告行 |

验证进度使用 `finished / planned`。在计划形成前可显示已知候选数。进入终态后，`finished` 保留实际获得结论的数量，计划内的其余项由 `skipped` 和 `cancelled` 表示。

### 快速诊断与预算预览

将与创建任务相同的请求体先 POST 到 `/scans/preview`，可读取验证计划和预算。该接口使用相同的参数校验，返回每个入口的协议、Token 上限、候选来源和已知目标数。任务层的 `target_count=null` 表示仍需发现。仅发现任务的验证目标数为 0。

响应中的 `max_requests` 是任务预算，`requests_remaining` 是发起预览时服务商的共享剩余额度，`effective_max_requests` 是两者中较小的值。`request_upper_bound` 还考虑可能的发现分页、验证目标及重试次数。预览读取本机配置和计数，可以在提交前刷新。当其他任务发出请求时，共享额度可能变化。

快速验证请求使用 `"mode":"quick"` 和单元素 `models`，多入口服务商须指定一个入口。`max_requests` 可省略或填 1。共享额度足够时，Agent 直接执行一次验证，重试为 0。批量验收可选手动候选、现有配置或自动发现。

### 两次扫描对比

`GET /scans/compare?baseline_id=旧任务ID&current_id=本次任务ID&endpoint_id=入口ID` 对比同一服务商的两条终态记录。入口必须在两次运行中都存在，且 ID 和协议一致。省略入口时，若只有一个共用入口，会直接对比。若有多个，返回 `endpoints` 供选择。

响应包含两条任务摘要、探测条件 `conditions`、目录覆盖 `catalog` 和逐模型 `rows`。只有两次实际得出验证结论，且可核对的连接和探测条件一致，才会报告通过转失败或失败转通过。必需条件有变化或无法确认时，保留两侧原始状态并标明原因。旧 TOML 记录可在其他条件一致时比较已观察到的结果，并提示凭据变化未知。托管连接的修订号变化会显示为配置变化。目录差异要求两个可比的完整 API 发现，手选列表和部分发现用于查看覆盖范围及未测项。

任务已淘汰或未知时返回 404，任务仍在执行时返回 409，同一任务或入口不兼容时返回 422。对比只读取已保存的记录。

### 按差异复测

`POST /scans/retest` 接收 `baseline_id`、`current_id`、`endpoint_id`、勾选的 `models` 和客户端生成的 `request_id`（可用 UUID）。Agent 会重新校验差异目标并跳过模型发现。探测参数取 `current_id` 的已保存快照，连接、请求策略和共享配额取当前服务商配置。

连接有变化时返回 409，响应包含 `code=connection_changed`、脱敏的 `changes` 和确认值 `confirmation`。确认后用原请求 ID 和相同目标提交，并带上该确认值。当前入口已删除或协议已变时返回 409，可以继续读取原有历史。

成功时返回 202 和新任务摘要。相同请求 ID、来源和目标集合在任务保留期间返回同一个任务，跨 Agent 重启仍然生效。用同一 ID 提交不同来源或目标会返回 409。任务摘要的 `source` 和 JSON 报告的 `scan.source` 提供两个来源任务 ID 及入口 ID。

## 5. 下载和取消

```powershell
Invoke-WebRequest "$agent/scans/$($job.scan_id)/report?format=json" -Headers $headers -OutFile report-agent.json
Invoke-WebRequest "$agent/scans/$($job.scan_id)/report?format=csv" -Headers $headers -OutFile report-agent.csv
Invoke-WebRequest "$agent/scans/$($job.scan_id)/report?format=xlsx" -Headers $headers -OutFile report-agent.xlsx
Invoke-WebRequest "$agent/scans/$($job.scan_id)/report?format=xlsx&scope=passed" -Headers $headers -OutFile report-passed.xlsx
Invoke-RestMethod "$agent/scans/$($job.scan_id)/cancel" -Method Post -Headers $headers
```

`OutFile` 由客户端执行。可以下载保留的完整报告及含发现或模型结果的部分报告。部分 JSON 使用 `scan.partial=true`，请求数未知时为 `null`。发现任务使用 JSON，XLSX 可用 `scope=passed` 只导出通过行。查不到任务时返回 404，任务没有可下载数据时返回 409。访问终态任务的取消端点会返回其现有状态。历史查询、下载和重复取消均只读取本地状态。[报告说明](reports-and-troubleshooting.md)

其他错误：401鉴权失败，403来源不允许，413请求过大，415格式不支持，422输入不符合约束，429任务容量满，503正在关闭。

## 6. 跨任务限额与生命周期

- 每Provider的并发、速率、429冷却和累计max_requests在所有任务之间共享。
- Provider累计请求预算从Agent启动计数，发现页和重试均计入；任务还有自己的预算和requests_sent，不会继承别的任务计数。
- 等待共享门控时取消不消耗请求预算；预算用尽则如实返回失败/跳过，不提供HTTP重置接口。修改注册表需重启，重启才重置累计预算。
- 默认任务执行截止最多300秒（不含排队）；更小的Provider配置优先。任务运行上限和等待容量均有限，历史只淘汰已结束任务。
- 启动恢复及每次任务终结时按结束时间清理历史，保留最近的 `--max-history` 个终态任务及其明细。排队、运行和取消中的任务不计入该上限，SQLite 可复用清理后释放的页面。
- Agent 将脱敏历史写到数据目录的 `scans.sqlite3`。Windows 默认使用 `%LOCALAPPDATA%\ModelScout`，macOS 使用 `~/Library/Application Support/ModelScout`，Linux 使用 `$XDG_DATA_HOME/ModelScout` 或 `~/.local/share/ModelScout`。每个数据目录允许一个 Agent 写入历史。要同时运行多个实例，可为它们指定不同的端口和数据目录。
- 正常停服会取消任务并提交终态。Agent 重启时将上次强制终止遗留的活动记录标为 `interrupted`，保留已提交的发现和结果。需要继续验证时可以新建任务。

## 7. OpenRouter自测

仓库提供[不含密钥的配置示例](../../examples/openrouter.toml)。先安全设置OPENROUTER_API_KEY，然后可执行：

```powershell
.venv/Scripts/python examples/agent_demo.py --config examples/openrouter.toml --provider openrouter --model YOUR_MODEL_ID
```

此模式会真实调用服务商，演示为每任务最多8次请求、45秒、256 tokens；必须显式提供模型。开发自测遵守已记录的[授权范围](../development/documentation-policy.md)。真实观察见[验证记录](../development/provider-tests.md)，不要把本地mock成功或HTTP任务completed等同于模型通过。
