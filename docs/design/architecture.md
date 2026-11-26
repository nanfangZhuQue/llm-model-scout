# 技术架构

适用版本：0.6.1 · 更新日期：2026-10-01

## 分层与职责

```text
CLI / 本地Agent
  → config + domain
  → core.scanner
      → discovery / verifier
      → adapters（OpenAI Chat / Anthropic Messages / OpenAI Responses）
      → transport（HTTPX + SOCKS5 + RequestGate）
  → report（脱敏JSON、原子文件输出）
```

- [domain.py](../../src/llm_model_scout/domain.py)：ProviderConfig、EndpointConfig、ProbeConfig、ScanPolicy、ModelResult、DiscoveryResult、ScanReport。
- [config.py](../../src/llm_model_scout/config.py)：CLI/env/TOML合并、URL规范化、输入校验和凭据解析。
- [adapters](../../src/llm_model_scout/adapters)：协议路径、认证、请求体、文本验证、发现分页解析；不负责调度。
- [transport.py](../../src/llm_model_scout/transport.py)：每入口独立AsyncClient，共享Provider请求门控；仅这里执行重试。
- [scanner.py](../../src/llm_model_scout/core/scanner.py)：发现入口候选、构造目标、固定worker与有界队列、取消和汇总。
- [report.py](../../src/llm_model_scout/report.py)：将安全报告对象输出为schema_version=1，不序列化运行期Endpoint凭据。

核心不读取环境变量、不操作终端UI、不调用Typer。Agent直接调用scan，不启动CLI子进程解析stdout。

## 主要接口

```python
load_provider(options, environ=None) -> ProviderConfig
await list_models(endpoint, transport) -> DiscoveryResult
await verify_model(endpoint, model_id, transport, result=None) -> ModelResult
await scan(provider, discover_only=False, include=None, exclude=None,
           on_event=None, http_transport=None) -> ScanReport
write_report(report, path, output_format="json") -> None
```

签名展示职责，完整类型和关键字参数以源码为准。http_transport用于测试注入，不改变生产默认网络路径；on_event可为同步或异步回调，事件输出已脱敏。

## 调度与生命周期

1. 为选中入口建立独立客户端，避免认证头跨入口共享。
2. 逐入口发现或读取手动候选；保留独立发现状态。
3. 过滤候选并构造(endpoint, model, profile)目标，应用模型上限后发出 `planned` 事件，再启动验证 worker。
4. 固定worker消费队列，请求经共享并发/速率/预算/冷却门控。
5. 验证响应并产生事件；最后排序和脱敏，返回完整或部分报告。

扫描核心按入口发出 `discovered`，其中 `count` 为该入口已知的候选数。`planned.count` 是纳入本次验证计划的目标数，仅发现任务跳过计划阶段。`result.status` 为 `available` 或 `unavailable` 时计作已验证。取消时 Agent 从部分报告补齐计划内未执行与在途取消的计数。超过模型上限的候选可留在报告中作为跳过行，验证分母取 `planned`。

一次请求使用HTTPX分阶段超时及asyncio.timeout总截止。退避在请求并发许可之外；429共享冷却，保证其他worker不会绕过等待。请求预算在发送前扣减，发现分页和重试均计入。

Ctrl+C或扫描截止会取消在途任务，保留已完成结果并标记未完成项。强杀进程、进程崩溃不具备数据库级恢复能力。

## 协议边界

配置显式选择 `openai_chat`、`anthropic_messages` 或 `openai_responses`。模型 ID 原样保留，调度和报告按入口区分同名模型。运行时不根据名称推断协议或回退到其他入口。

模型列表解析支持data[].id与has_more游标；Anthropic使用after_id，OpenAI兼容分页使用after。不兼容发现格式由用户手动提供候选；不能无限猜测路径。

## 0.2.0探测预算与诊断

`config.load_provider` 在命名 `ProbeConfig` 基础上应用 `max_tokens` 覆盖，校验正整数。`ProbeConfig` 也校验直接库调用。实际值进入入口的 `probe` 报告。

适配器validate_text返回白名单停止原因或抛出携带固定诊断码/建议的InvalidResponse；verifier写入ModelResult的diagnostic_code、suggestion、stop_reason。report通过数据类序列化新增可空字段，schema_version仍为1；旧客户端应忽略未知字段。网络/HTTP错误不推测文本失败原因，未知响应字段、reasoning和拒绝正文均不导出。

## 0.3.0 CSV输出

report_csv只接收已脱敏的ScanReport，固定字段展开每个模型结果；write_report统一完成JSON/CSV原子写入。CLI的--format仅影响--out，stdout仍是完整JSON。CSV使用UTF-8 BOM及标准库转义，危险文本单元格前加单引号，不改变JSON或原始报告。CSV为空只有表头，发现详情和完整汇总仍在JSON中。

## 0.4.0本地Agent

agent/config.py加载已批准Provider注册表及明确的凭据引用；agent/app.py定义FastAPI schema、Host/Origin/token边界和下载接口；agent/manager.py维护有界内存任务、进度、取消和历史淘汰。服务只绑定127.0.0.1，扫描/下载请求不能提供服务器文件路径；0.6.1的鉴权服务商管理接口专门接收用户输入的URL、Key和代理。

Agent 每个服务商持有长生命周期的 RequestGate。Transport 在同一个锁内检查并扣减服务商累计预算和单任务预算。等待取消时保留额度，429 冷却跨任务传播。报告的 `requests_sent` 记录当前任务。重启时运行期额度重新计数。

运行、排队、终态历史默认上限为 2、16、50。任务默认最多执行 300 秒，也可用配置降低上限。关闭时取消并等待已创建任务。鉴权任务列表和 `scan_id` 查询可读取本次会话任务及 SQLite 中保留的历史。[Agent 教程](../user-guide/agent.md)

## 0.5.0同源Web UI

web/index.html、app.css、app.js、favicon.svg随包交付。Agent使用固定路由闭包读取明确文件，不挂载工作目录；仅这些静态GET/HEAD公开，数据API继续鉴权，所有响应加同源CSP及no-referrer。GET /scans提供最新优先分页摘要，/providers增加安全的入口默认值和参数上限。

前端不保存凭据，使用相对URL和Bearer头请求。单轮定时刷新由完成后再调度，会话代次和任务选择代次防止过期响应回写；退出/切换任务会取消读取。模型/错误经textContent渲染，报告经鉴权fetch得到Blob后下载。无需Node构建或第三方CDN；浏览器异常状态与UI教程见[Web UI文档](../user-guide/web-ui.md)。

## 0.6.1直接管理与加密保存

providers.py规范化连接定义并用SQLite保存密文；Windows通过用户DPAPI，其他系统需受信任OS keyring中的Fernet密钥，不允许明文降级。SQL只接收加密后的JSON，修订号防止并发覆盖，用户数据默认在应用数据目录。

ProviderService在鉴权管理接口与运行时registry间同步，更新不重置已有gate；同一Agent活动任务未结束时拒绝编辑/删除。Job保留已脱敏的显示名称，删除服务商后历史报告仍可查询。模型发现通过operation=models复用scan(discover_only)，所选模型通过model_selections按入口提交。

普通报告不含Key；POST配置导出只有显式include_secrets和confirm_plaintext都为true才返回含Key附件。新增interactive/desktop命令和BAT入口；GUI/CLI共用本机存储。旧--config启动保持只读兼容模式。

### XLSX 报告

Agent 下载端点选择全部结果或仅通过结果，再交给 `report_xlsx` 序列化。生成器写完表头和数据后，根据最终工作表范围设置自动筛选。只有表头的工作簿也会设置对应范围。

## 扫描历史存储与查询

`agent/history.py` 使用 SQLite `user_version=3`。版本 1 在迁移时回填服务商 ID、创建及结束时间和索引。版本 3 新增复测请求表，版本 1 和 2 均可在启动时事务迁移。任务、入口发现和模型结果分别建表，明细按任务内序号保存，使脱敏后同名的结果仍各自保留。探测条件快照包含所选入口、候选范围、探测配置、请求策略和托管服务商修订号。终态报告由保存的报告头和有序明细重建。

Agent 在接受任务前提交初始记录。每次发现或模型结果与对应进度在同一个事务内提交，终态连同最终报告一次提交。接受阶段写入失败返回 503。检查点故障会停止扫描并保留上次已提交的数据。终态提交失败时，进程中的任务显示保存失败，磁盘回读以最近一次成功提交为准。

每个数据目录由一个 Agent 持有历史写入锁，进程退出时由操作系统释放。Agent 启动时将上次遗留的排队、运行或取消中任务标为 `interrupted`，保留已提交的明细。启动恢复和终态提交都按结束时间保留最近的 `max_history` 条终态任务，并级联删除被淘汰的明细。活动任务不占用终态配额。

`GET /scans` 在同一 SQLite 读取事务中计算分页、总数和服务商选项，按创建时间和扫描 ID 倒序排列。查询单个任务的摘要不读取模型明细。下载已提交的终态报告时使用保存的版本和数据。只有检查点时，报告由已保存的发现和模型结果生成，JSON 标记 `scan.partial=true` 和 `requests_sent=null`。历史查询与下载均为本地读取。

## T05 两次扫描对比

`GET /scans/compare` 在同一个 SQLite 读取事务中加载两条保留记录，避免保留策略运行时混用两个版本的快照。纯函数按服务商、入口 ID 和协议校验配对，再比较储存版本中的有效连接与探测条件。只有成对的已验证结果可以形成状态变化结论。目录覆盖与验证结论是两个独立字段，手选、截断或失败的发现不会生成目录增减。对比页不会连接上游。

## T06 按差异复测

`POST /scans/retest` 使用已保存的两条快照重新计算差异和可选模型。任务管理器复用 `model_selections` 创建单入口任务，并传入本次来源保存的 `ProbeConfig`。实时连接、运行策略及共享门控来自当前服务商。连接差异确认值绑定本次请求和当前运行中的配置差异。

历史在同一 SQLite 事务中插入任务、来源和请求 ID 去重记录。去重记录随复测任务的保留策略清理。来源 ID 作为任务数据保留，即使原任务日后被淘汰。Agent 提交路径在最后一次去重检查和建任务之间不挂起。

## T07 验证模式和预算预览

`POST /scans/preview` 与创建任务共用入口选择、模型来源和预算校验。预览函数读取解析后的策略及服务商门控剩余额度，返回协议、目标数、Token 上限和请求上界。自动发现时按允许的发现页数与模型上限计算请求上界。手动候选按入口去重，同名模型在不同入口分别计数。

快速模式使用单入口、一个显式模型、一次请求和零重试。批量模式沿用当前候选和请求策略。两者复用扫描器、共享门控和既有诊断报告。

## T08 Responses 非流式文本探测

`adapters/openai_responses.py` 使用默认 Bearer 认证请求 `POST /responses`。请求体发送 `input`、`max_output_tokens`、`stream=false` 和 `store=false`，仅在配置给出时发送 `temperature`。[OpenAI Responses API 参考](https://developers.openai.com/api/reference/resources/responses/methods/create)

在 HTTP 200 下，响应须为 `response` 对象，状态为 `completed`，`output` 中须包含有效的 assistant 消息和非空 `output_text`。已有部分文本的未完成响应仍给出 `incomplete_response`。拒答、空文本和上游业务报错分别给出 `refusal`、`empty_text` 和 `response_error`。只持久化固定诊断与白名单停止原因。

托管连接沿用加密定义，可按协议各保存一个入口，共享服务商请求门控。报告与历史已按入口和协议存储结果，因此新增协议可以沿用现有存储版本及报告版本。

## T09 流式文本探测

显式选择 `capability=stream` 时，三种适配路径均沿用各自认证并协商 `text/event-stream`。每个目标的一次流式尝试占用一次请求预算。以 `chat-basic`、16 Token 为例，将下列路径追加到已配置的 Base URL。

| 请求路径 | 请求体 |
|---|---|
| Chat `POST /chat/completions` | `{"model":"MODEL_ID","messages":[{"role":"user","content":"ping"}],"max_tokens":16,"stream":true}` |
| Messages `POST /messages` | `{"model":"MODEL_ID","messages":[{"role":"user","content":"ping"}],"max_tokens":16,"stream":true}` |
| Responses `POST /responses` | `{"model":"MODEL_ID","input":"ping","max_output_tokens":16,"stream":true,"store":false}` |

| 协议 | 文本证据 | 通过所需的完成证据 |
|---|---|---|
| OpenAI Chat | 至少一个未拒绝的选项含非空 `delta.content` | 选项提供 `finish_reason`，且收到 `data: [DONE]` |
| Anthropic Messages | assistant 消息的文本块含非空文本 | 块关闭，收到含 `stop_reason` 的 `message_delta`，再收到 `message_stop` |
| OpenAI Responses | 非空 `response.output_text.delta`，最终响应含非空 assistant `output_text` | 收到 `response.completed`，所带响应状态为 `completed` |

[OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) · [Anthropic 流式消息](https://platform.claude.com/docs/en/build-with-claude/streaming) · [OpenAI Responses 流事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)

Transport 复用每入口 HTTP 客户端、共享门控和单次请求超时。SSE 按块读取并处理 UTF-8 分片、注释及多行数据。请求协商无压缩响应，读取累计字节默认上限为 2 MiB，未形成完整事件的数据也占额度。等待期间可取消，重试从空的解析状态开始。报告只保存状态、诊断、停止原因、尝试次数和计时，无原始流文本。

一次任务选择一种能力。Agent 预算预览返回能力和按入口展开的目标数。历史按能力区分比较，旧记录缺省为 `text`。流式任务标记 `stream`，复测继承来源探测的能力。SQLite 和报告版本沿用现有契约。

## T10 结构化输出探测

`capability=structured` 使用非流式请求，要求模型返回 `{"status":"ok","count":1}`。固定 Schema 要求对象包含 `status`（值为 `"ok"`）和 `count`（整数 JSON 数值），并禁止额外属性。下面列出附加到普通探测请求的字段，其中 `SCHEMA` 表示该固定 Schema 对象。

| 协议 | 请求扩展 |
|---|---|
| OpenAI Chat | `response_format: {type: "json_schema", json_schema: {name: "scout_structured_v1", strict: true, schema: SCHEMA}}` |
| OpenAI Responses | `text: {format: {type: "json_schema", name: "scout_structured_v1", strict: true, schema: SCHEMA}}` |
| Anthropic Messages | `output_config: {format: {type: "json_schema", schema: SCHEMA}}` |

[OpenAI 结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs) · [Anthropic 结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)

适配器复用各协议的文本、拒答和结构验证，再解析完整 JSON 并在本机校验。无效 JSON 报 `invalid_structured_json`，有效但不符合 Schema 报 `schema_mismatch`。检测到拒答或截断时沿用对应诊断。普通、流式和结构化模式都各自使用一次请求，不会因验证失败追加另一种能力的探测。

结构化任务及结果行保存 `capability=structured`，探测条件记录实际固定提示词。预算预览沿用解析后的目标数、请求次数和重试约束。历史比较按能力配对，复测沿用来源能力。CSV 和 XLSX 沿用已有列并在探测配置值中标注 `(structured)`。

## T11 工具调用探测

`capability=tool` 发送非流式请求，要求生成且只生成一次 `scout_tool_probe` 调用。固定参数 Schema 要求 `value` 为 `"ping"`、`count` 为整数，禁止额外属性。提示词给出的示例参数是 `{"value":"ping","count":1}`。下表的 `SCHEMA` 表示该固定参数对象。

| 协议 | 工具和选择请求 | 完成与调用证据 |
|---|---|---|
| OpenAI Chat | `tools: [{type: "function", function: {name: "scout_tool_probe", parameters: SCHEMA, strict: true}}]`，`tool_choice: {type: "function", function: {name: "scout_tool_probe"}}`，`parallel_tool_calls: false` | `finish_reason=tool_calls`，assistant `message.tool_calls` 中恰好一个函数调用 |
| OpenAI Responses | `tools: [{type: "function", name: "scout_tool_probe", parameters: SCHEMA, strict: true}]`，`tool_choice: {type: "function", name: "scout_tool_probe"}`，`parallel_tool_calls: false` | 响应 `status=completed`，`output` 中恰好一个 `function_call` |
| Anthropic Messages | `tools: [{name: "scout_tool_probe", input_schema: SCHEMA, strict: true}]`，`tool_choice: {type: "tool", name: "scout_tool_probe", disable_parallel_tool_use: true}` | assistant 消息 `stop_reason=tool_use`，`content` 中恰好一个 `tool_use` |

[OpenAI 函数调用](https://developers.openai.com/api/docs/guides/function-calling) · [Anthropic 工具选择](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools#forcing-tool-use)

OpenAI 的参数 JSON 字符串和 Anthropic 的原生参数对象均用精确 JSON 数值校验。报告保留固定诊断、HTTP 状态和类别，省略上游返回的调用原文、调用 ID 与参数。探测在验证此次响应中的调用证据后结束。

预算预览、历史和复测沿用其他能力的契约。新任务及结果行使用 `capability=tool`，CSV/XLSX 在原探测配置值中标注 `(tool)`。上游不支持指定工具时保留 HTTP 错误证据。

## 安全边界

生产TLS验证开启，不跟随重定向，不继承环境HTTP代理，不在SOCKS失败后直连。Key通过引用解析到内存，repr隐藏；错误只提取有限字段并脱敏，CLI不打印底层异常原文。响应读取有大小上限，深层解析异常不会导致整个扫描崩溃。

源码包使用显式内容白名单，不打包.mcp.json、.claude、.opencode、扫描报告等个人配置。文档分类后仅纳入明确的公开文档目录，历史归档排除，详见[开发指南](../development/development-guide.md)。

## 未来扩展（未实现）

后续可扩展历史对比和远程多用户部署。本地 Agent 实施 Host/Origin、会话 Token 与任务限额，UI 使用同源调用。增加协议时新增适配器与契约测试，修改报告契约时同步 schema 版本和迁移说明。
