# 报告与故障排查

适用版本：0.6.1，schema_version=1 · 更新日期：2026-10-01

报告与连接配置导出分开：普通JSON/CSV报告不包含Key。0.6.1的“导出配置”在明确勾选并确认后可包含Key和代理密码，见[快速使用](quick-start.md)；这是用户主动的敏感文件导出，不是日志或普通报告的默认行为。

## 报告结构

| 字段 | 含义 |
|---|---|
| schema_version | 报告契约版本；不是应用版本 |
| tool | 工具名称及版本 |
| provider.name / endpoints | 服务商与入口ID、协议、规范化地址、probe摘要；不包含Key |
| scan | 状态、原因、开始/结束UTC时间、requests_sent及policy |
| discovery | 各入口source、status、models、pages、HTTP状态与错误 |
| results | 各(endpoint, model, profile)的验证结果 |
| summary | 已列出验证目标的总数及四类状态计数 |

结果按endpoint_id、model、probe_id排序。列表获取失败/被上限截断时，工具不知道的模型不会凭空出现在results，不能用summary推算服务商所有模型数。models命令没有验证目标，summary.total=0是正常情况。

## 状态

- available：当前探测通过。
- unavailable：当前探测已经得到失败结论；不是永久不可用。
- skipped：没有发出验证请求，例如请求预算或目标预算耗尽。
- cancelled：已发起但取消/截止时没有完整结论。

扫描completed仅表示任务完整执行，可以包含unavailable。failed可能由发现不完整、预算、扫描截止时间等导致；cancelled表示用户取消。discovery还可能显示partial，已有候选仍会被验证。

模型结果的reason解释执行状态，例如probe_passed、probe_failed、request_budget_exhausted、retry_budget_exhausted、model_budget_exhausted、scan_cancelled。

## 时间与尝试次数

- attempts：实际发出的验证请求次数；不含发现。
- latency_ms：最后一次验证尝试的耗时；未开始/取消时可能为null。
- elapsed_ms：整个单模型验证过程，包含排队、退避和重试等待；不是性能基准。
- timestamp：结果时间，UTC ISO格式。
- http_status：没有收到HTTP响应时为null；收到响应头后再超时可能同时存在HTTP状态和TIMEOUT。

## 错误排查

| error_category | 排查方向 |
|---|---|
| OK | 当前profile通过，不代表其他能力已验证 |
| NETWORK_ERROR | DNS、连接、TLS、代理端口/鉴权；指定SOCKS后不会直连 |
| TIMEOUT | 检查服务响应速度和网络；适当增大timeout/scan-timeout |
| UNAUTHORIZED | 检查对应入口的Key及认证方式；不要将Key贴入日志 |
| FORBIDDEN | 模型权限或入口访问限制，不等于另一入口也失败 |
| PAYMENT_REQUIRED | 余额、额度或服务商付款限制 |
| MODEL_NOT_FOUND | 错误体明确指向模型不存在；检查大小写和准确ID |
| RATE_LIMITED | 降低rate-limit/concurrency，检查账户请求限额 |
| SERVER_ERROR | 服务商/网关5xx，包含部分提供商的529 |
| BAD_RESPONSE | HTTP200但JSON/协议/非空文本校验失败；检查profile和响应兼容性 |
| UNKNOWN_ERROR | 非标准错误、路径404、参数400等；根据脱敏message定位 |

### HTTP200但BAD_RESPONSE怎么办？

先检查 `diagnostic_code` 和 `stop_reason`。Chat/Messages 的 `token_limit_without_text` 与 Responses 的 `incomplete_response` 搭配 `max_output_tokens` 都可定位到长度限制。其他停止原因应按实际证据处理。可用显式 Token 上限配合 `--retries 0 --max-requests 1` 小范围复测。

### 列表接口404怎么办？

先确认Base URL，没有误填完整聊天端点。若服务商没有实现列表接口，使用--model或--models-file直接验证聊天；不要把发现失败判成所有模型不可用。

### 返回码为0但模型失败？

默认是正常行为。要让自动化检查因模型失败而失败，开启--fail-on-unavailable。

| 退出码 | 含义 |
|---|---|
| 0 | 任务完整执行，可能包含模型失败 |
| 1 | 配置校验失败、网络初始化失败或任务不完整 |
| 2 | Typer参数语法错误，或--fail-on-unavailable发现失败 |
| 130 | 用户取消 |

## 结构化诊断（0.2.0新增）

results新增diagnostic_code、suggestion、stop_reason可空字段。原status/error_category/reason保持；schema_version仍为1，消费者应容忍新增字段。旧报告没有这些字段时按未知处理，不回填推测。

| diagnostic_code | 观察依据 |
|---|---|
| invalid_structure | JSON/消息结构无效，或响应超出读取上限 |
| empty_text | 没有非空文本，且没有更具体证据；原因尚未确定 |
| token_limit_without_text | 无文本且停止原因为OpenAI length或Anthropic max_tokens |
| non_text_output | 只观察到reasoning/tool_calls或非text内容块，不等于文本验证通过 |
| refusal | Chat 的 content_filter/非空 refusal 字段、Responses 的拒答块，或 Anthropic 的 refusal 停止原因 |
| incomplete_response | Responses 尚未完成。已有部分文本也判为失败，已知原因记入 `stop_reason` |
| response_error | Responses 虽返回 HTTP 200，但响应对象包含错误或状态为 `failed` |
| empty_stream | 空流，或完整流中没有非空 assistant 文本 |
| stream_interrupted | 读流断连，或连接已结束但尚未收到协议要求的完成证据 |
| stream_limit_exceeded | 完成前流式正文超过累计读取上限 |
| invalid_stream | SSE 类型、编码、事件格式或协议顺序无效 |
| invalid_structured_json | 助手文本未包含单个合法 JSON 值，例如格式错误、重复属性或带 Markdown 围栏 |
| schema_mismatch | JSON 可以解析，但根类型、属性、必填字段或字段类型不符合固定 Schema |
| no_tool_call | 响应没有返回合成工具调用，例如只返回普通文本 |
| unexpected_tool_name | 返回的名称与合成工具名称不一致 |
| invalid_tool_arguments | 参数 JSON 无法解析，或属性和值不符合固定 Schema |
| unexpected_tool_call_count | 响应返回多个工具调用，超出本次探测规定的一个 |

这些诊断归属BAD_RESPONSE，不新增error_category。suggestion是固定安全提示，不包含供应商原始文本；stop_reason只输出已识别的协议枚举，未知值为null。HTTP错误和未执行结果通常没有文本诊断。

Chat 和 Messages 有非空有效文本时，可在 `length` 或 `max_tokens` 终止下通过。Responses 要求响应状态为 `completed`，且包含非空 assistant 文本。普通报告只记录固定诊断及白名单停止原因，省略推理文本、拒答正文和原始错误载荷。

流式、结构化或工具调用结果的 JSON 行分别标记 `capability: "stream"`、`capability: "structured"` 或 `capability: "tool"`。CSV 和 XLSX 在「探测配置」值末尾显示 `(stream)`、`(structured)` 或 `(tool)`。流式连接异常可保留 `NETWORK_ERROR` 并同时记录 `stream_interrupted`。用户取消由任务和模型的取消状态表示，已收到但尚未完成的文本不计为通过。

XLSX 中的服务商名称、模型 ID 和错误信息按文本单元格写入。类似公式的模型 ID 保留原值，打开文件时作为文本读取。

## CSV导出（0.3.0新增）

scan/verify使用--format csv --out report.csv；stdout依然输出完整JSON。CSV每行一个验证目标，按endpoint/model/profile排序；无结果时仅表头，完整发现失败与任务摘要须看JSON，不能把空CSV视为成功。

固定列顺序：csv_schema_version、tool_version、provider、endpoint_id、protocol、base_url、scan_status、scan_reason、model、probe_id、max_tokens、status、http_status、error_category、error_message、reason、diagnostic_code、suggestion、stop_reason、attempts、latency_ms、elapsed_ms、timestamp。csv_schema_version当前为1，与JSON schema_version独立。

文件使用UTF-8 BOM、CRLF行结束符、标准CSV引号转义；空值为空单元格，数字保持数值表示。中文、逗号、双引号和单元格内换行经标准库往返测试，未声称已在实际Excel应用中人工验证。

文本若以=、+、-、@开头（包括前导空白/不可见字符），或直接以制表符/换行开头，会添加单引号降低公式执行风险；不会修改JSON和内存原报告。CSV不适合无损保存原值，重新编辑/另存或去掉防护字符可能重新引入风险；需要精确数据请用JSON。

## XLSX导出（0.6.1新增）

Web UI任务详情的“下载 XLSX（全部）/下载 XLSX（仅通过）”和Agent报告接口`format=xlsx`生成Excel工作簿（依赖openpyxl）。模型发现任务仍仅支持JSON。

与CSV的差异：

- 列精简：去掉csv_schema_version、tool_version、suggestion三列，其余列顺序与CSV一致；表头为中文。
- 值翻译：扫描状态/模型状态/错误分类/失败原因/停止原因等枚举值转为中心词中文（如available→通过、PAYMENT_REQUIRED→需付费、probe_failed→探测失败）；error_message保留服务商原文，不翻译动态文本。JSON与CSV仍保留原枚举值。
- 单位与格式：延迟、耗时转换为秒（保留3位小数）；时间戳转换为本地时间并精确到秒（YYYY-MM-DD HH:MM:SS）。
- 首行冻结，自动筛选范围覆盖表头与本次导出的全部数据行。通过（available）的行整行标绿，精确原值可查阅 JSON。
- 范围与命名：`scope=passed`仅导出通过行（无通过结果时为仅表头），scope只对xlsx生效；文件名为“服务商名称-YYYYMMDD-HHMMSS[(-仅通过)].xlsx”，时间取任务完成/创建时间的本地秒，服务商名称中的文件系统非法字符替换为下划线，中文文件名经filename*下发并提供scan-id回退名。

## 历史与部分报告

Agent 的保留历史可以在重启后下载。任务仅留下检查点时，JSON 的 `scan.partial=true` 表示报告只覆盖已提交的发现或验证结果，`scan.requests_sent=null` 表示实际请求数未知。进程停止造成的中断使用 `interrupted`，JSON 和 CSV 下载名带 `-partial`，XLSX 下载名带「部分」。连检查点也没有的任务在详情中仍可查询，下载返回 409。

## 文件输出

`--out` 默认保存 JSON，可用 `--format csv` 保存 CSV。stdout 默认输出 JSON，启用 `--pretty` 时输出表格汇总。父目录须已存在，覆盖已有文件需指定 `--overwrite`。工具通过临时文件和原子替换写入。自动化应检查退出码和目标文件。Ctrl+C 时会尝试保存部分报告。

报告仍可能包含服务商地址、模型名称等业务信息，分享前自行确认；真实测试摘要见[服务商验证记录](../development/provider-tests.md)。
