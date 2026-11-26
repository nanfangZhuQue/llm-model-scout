# 需求说明

文档版本：1.6 · 当前应用：0.6.1 · 规划更新：2026-11-26

## 1. 背景和目标

第三方模型服务商暴露的模型列表不等于当前账号可调用的模型。用户提供服务商地址、凭据引用及可选SOCKS5代理，工具发现候选模型并发送最小真实文本聊天请求，输出可解释、可导出的探测结果。

目标用户为需要排查模型权限、服务商兼容性和代理网络的本地工具使用者。结果只证明“当前凭据、地址、协议、参数、网络和时间下这次请求是否通过”，不证明模型身份，也不保证持续可用性。

## 2. 当前范围

| 需求ID | 需求与验收条件 | 当前状态 |
|---|---|---|
| REQ-001 | 提供models、verify、scan及version/help命令；CLI不承载网络业务逻辑 | 已实现 |
| REQ-002 | 同一Provider可配置OpenAI Chat Completions与Anthropic Messages入口；同名模型分别验证 | 已实现 |
| REQ-003 | CLI、环境变量、TOML配置；密钥优先引用环境变量；多入口必须显式选择 | 已实现 |
| REQ-004 | 获取模型列表；支持分页；列表不可用时可显式提供手动模型；不能臆造候选ID | 已实现 |
| REQ-005 | 文本探测要求 HTTP 200、有效协议结构及非空 assistant 文本；不自动跨模型或协议回退 | 已实现 |
| REQ-006 | 固定worker并发，超时、退避重试、请求预算与共享限流 | 已实现 |
| REQ-007 | 发现和验证都走指定SOCKS5，支持鉴权与HTTPS，失败不回退直连 | 已实现并本地链路验证 |
| REQ-008 | 输出版本化JSON，包含协议入口、模型、状态、错误、尝试次数和时间 | 已实现 |
| REQ-009 | 密钥、代理凭据不进入日志、异常原文和报告；报告写入原子化 | 已实现并覆盖测试 |
| REQ-010 | 取消时保留已完成项，未执行和在途取消不得计为不可用 | 已实现并覆盖核心取消测试 |
| REQ-011 | README、示例配置、单元/集成/CLI端到端测试和本地mock演示 | 已交付 |
| REQ-012 | 兼容Windows/macOS/Linux | Windows源码、原生浏览器及干净wheel安装已验收；跨平台CI实际运行仍待环境支持 |
| REQ-013 | 文档分类管理；需求变化同步需求、设计、教程、进度与变更记录 | 已建立 |
| REQ-014 | 显式覆盖探测token上限，输出基于响应证据的安全诊断；不自动增大预算或重新调用服务商 | 已实现并通过本地回归 |
| REQ-015 | scan/verify通过--format csv --out导出固定列CSV，处理中文、引号和公式注入；stdout保持完整JSON | 已实现并通过本地及真实失败报告验收 |
| REQ-016 | 本地Agent加载注册表，鉴权及Host/Origin防护；创建/查询/取消任务、下载报告；跨任务共享Provider限额 | 已实现并通过本地及真实链路验收 |
| REQ-017 | 同源Web UI：会话连接、扫描表单、任务列表/取消、结果筛选、报告下载及响应式布局；不保存浏览器凭据 | 已实现并通过本地接口及浏览器验收 |
| REQ-018 | 无配置输入/编辑服务商、自动本机加密保存、独立模型发现与勾选测试、显式导出包含Key的连接配置 | 已实现并通过Windows本机与原生浏览器验收 |
| REQ-019 | Windows Web UI和CLI双BAT快捷启动，环境准备与路径/错误处理 | 已实现并通过Windows启动测试 |
| REQ-020 | .gitignore分类注释，过滤本机凭据、数据库、导出与测试产物 | 已实现 |

## 3. 协议和验证规则

- `openai_chat`：验证规范化根路径下的`chat/completions`；标准Bearer认证；读取choices中的assistant消息文本。
- `anthropic_messages`：验证根路径下的`messages`；标准x-api-key及anthropic-version；读取content数组的text块。thinking/tool_use不算文本成功。
- `openai_responses` 请求根路径下的 `responses`，默认使用 Bearer 认证。只在 HTTP 200、响应已完成且包含非空 assistant `output_text` 时通过。拒答、空文本、响应报错和未完成分别诊断。
- 模型发现按入口独立执行；模型名称不能决定协议。Anthropic兼容入口缺失models接口不证明Messages不可用。
- 同一模型在两个入口是两个探测目标；不得把一侧成功复制到另一侧。
- 不支持的参数、输出预算不足、错误路径都可能使探测失败；报告不能宣称模型永久不可用。
- 显式拒绝/过滤单独诊断为refusal；没有文本且明确长度终止才标token_limit_without_text。其余按结构、空文本、非文本证据分类；未知停止原因不输出原值。新增诊断字段不改变原错误枚举，不导出推理或拒绝正文。

## 4. 默认值与预算

| 设置 | 默认值 | 约束 |
|---|---|---|
| profile | CLI/TOML 的 Chat 和 Messages 默认 `baseline`。Responses 默认 `chat-basic` | `baseline` 发送非流式 ping，Token 上限 1，temperature 为 0 |
| chat-basic | CLI/TOML 的 Responses 默认 16 Token，托管入口默认 256 Token | 发送非流式 ping，省略 temperature |
| max_tokens覆盖 | 未设置 | 正整数；CLI > SCOUT_MAX_TOKENS > Endpoint TOML > profile默认，不自动调整 |
| concurrency | 10 | Provider内全部选中入口共享 |
| timeout | 30秒 | 单次HTTP完整请求截止，不含排队和退避 |
| retries | 2 | 额外两次，最多三次尝试 |
| max_requests | 1000 | 包含发现页、验证与重试 |
| max_models | 1000 | 验证目标总上限，同时限制单入口发现保留数量 |
| max_pages | 100 | 每入口发现页上限 |
| rate_limit / scan_timeout | 未设置 | 用户可显式限制速率/扫描总时长 |

仅429、5xx、超时重试；已知401/402/403/404不重试。Retry-After与共享冷却生效；没有剩余预算时不继续等待重试。超时可能已经被上游处理和计费，工具不能保证exactly-once或精确金额。

## 5. 结果和异常

模型状态为available/unavailable/skipped/cancelled；扫描状态为completed/failed/cancelled。已完成但探测失败与整个任务执行失败必须分开统计。具体字段和退出码见[报告说明](../user-guide/reports-and-troubleshooting.md)。

统一错误枚举为OK、NETWORK_ERROR、TIMEOUT、UNAUTHORIZED、FORBIDDEN、PAYMENT_REQUIRED、MODEL_NOT_FOUND、RATE_LIMITED、SERVER_ERROR、BAD_RESPONSE、UNKNOWN_ERROR。没有明确模型不存在证据的404用UNKNOWN_ERROR，不新增与实现不一致的枚举。

## 6. 非目标与候选后续范围

CSV从0.3.0纳入当前需求，仅导出模型验证结果；models命令仍为JSON，完整发现/汇总保留在stdout JSON。CSV为空时仅表头，不代表扫描成功；文本为防公式注入可添加单引号，精确原值以JSON为准。

账号体系、云端存储、多人协作、模型身份鉴伪和复杂性能基准仍属候选范围。扫描历史与新增探测能力进入第二阶段计划，交付状态见第 8 节。

本地 Agent 复用扫描核心，仅监听 127.0.0.1，并实施会话 Token、Host/Origin 和请求体限制。运行、排队和会话内任务列表设有上限。服务商请求预算按 Agent 启动后累计，任务独立计数。具体接口和限额见[Agent 教程](../user-guide/agent.md)。

0.6.1根据用户确认增加服务商自动本机保存：Windows DPAPI加密后写SQLite，其他平台需OS安全密钥库，不做明文降级。配置无需手工编辑；普通报告仍脱敏，只有明确确认的配置导出允许含API Key/代理密码。服务商管理接口允许用户输入URL/Key/代理，扫描与下载仍不接受任意服务器文件路径。详见[快速使用](../user-guide/quick-start.md)。

## 7. 验收与追踪

0.5.0将同源Web UI纳入当前范围，由Agent提供固定静态资源与鉴权API；新增任务列表和参数上限概要。页面Token只在内存，不通过URL/浏览器存储传递，模型和错误按纯文本渲染；详见[Web UI教程](../user-guide/web-ui.md)。

每条需求有稳定ID；新需求按序追加。范围/默认值/协议/报告字段变化，先记录[变更条目](../development/changelog.md)，再按[维护规则](../development/documentation-policy.md)同步相关文档和测试。实现与验收状态见[开发进度](../development/progress.md)，不能仅凭mock验证宣称真实服务商可用。

## 8. 第二阶段需求

REQ-021 至 REQ-024 已用于 0.6.1 体验优化，原始范围见[变更记录](../development/changelog.md)。以下编号对应[第二阶段开发任务](../development/phase-2-plan.md)。

| 需求 | 用户可验收的行为 | 状态 |
|---|---|---|
| REQ-025 | 全部与仅通过 XLSX 的筛选覆盖实际数据，浏览器下载能回读验证 | 已修复并通过本地回归 |
| REQ-026 | 多入口发现与验证分别显示进度，取消和预算耗尽显示已执行数量 | 已实现并通过本地回归 |
| REQ-027 | 本机保存脱敏 Agent 扫描历史，重启后按服务商分页查询及下载完整或已保存部分报告，按终态任务数量清理历史 | T03 和 T04 已实现并通过本地回归 |
| REQ-028 | 比较同一服务商同入口两次扫描，区分验证变化、目录变化、条件变化或无法确认及未测 | 已实现并通过本地回归 |
| REQ-029 | 按入口勾选差异项，沿用探测参数创建可追溯的复测任务 | 已实现并通过本地回归 |
| REQ-030 | 工作台可预览预算，直接验证指定模型或执行批量验收，并查看诊断证据 | 已实现并通过本地回归 |
| REQ-031 | 显式选择 OpenAI Responses 入口进行独立的非流式文本验证 | 已实现，见 T08 验收 |
| REQ-032 | 可单独选择流式、结构化输出、工具调用探测，分别查看能力结果 | T09 至 T11 已实现 |

流式文本覆盖 OpenAI Chat、OpenAI Responses 和 Anthropic Messages。各协议收到非空 assistant 文本和规定的终止事件后才可通过。空流、终止前断连、达到读取上限后仍有数据、无效事件与用户取消分别有可查结论。历史对比只对同种探测能力计算变化，同一入口的旧报告按非流式文本读取。

结构化输出使用三种协议各自的 JSON Schema 请求格式。工具验证完整文本是否为单个合法 JSON 值，并检查 `status` 与 `count` 的属性和值类型。可解析 JSON 但字段类型错误时单独诊断。结构化探测需显式选择，每个目标的一次尝试计为一次请求。

工具调用探测显式请求一个无副作用的合成函数，校验协议是否完成、调用数量、工具名和参数。纯文本回复、错误工具名、错误参数和多次调用分别给出诊断。探测在验证返回指令后结束。
