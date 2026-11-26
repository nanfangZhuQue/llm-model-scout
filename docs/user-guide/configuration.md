# 配置与命令参考

适用版本：0.6.1 · 更新日期：2026-10-01

## 0.6.1推荐入口

无需配置文件时双击start-web.bat，或运行`model-scout desktop`；直接输入服务商信息并加密保存。`start-cli.bat`进入交互菜单。以下参数表主要描述兼容的底层命令，最新直接操作流程见[快速使用](quick-start.md)。

## 命令

| 命令 | 用途 |
|---|---|
| models | 输出候选列表及发现状态，不验证聊天能力 |
| verify | 恰好一个入口、一个显式手动模型，强制并发1 |
| scan | 对选中入口发现或手动指定的模型执行验证 |
| agent | 本地鉴权HTTP任务服务，独立启动参数见[Agent教程](agent.md) |
| desktop / interactive | 无配置Web/CLI入口，支持--data-dir指定本机加密数据目录 |
| version / --help | 版本和帮助 |

参数放在子命令后。三个扫描子命令共用下列参数定义；Agent为独立参数集，启动注册表只解析明确的凭据环境引用，不应用普通SCOUT_*网络覆盖。verify额外要求一个手动模型。models不产生验证结果，summary.total可为0，应看discovery.models。

## 参数、环境变量与默认值

| CLI参数 | 环境变量 | 默认/说明 |
|---|---|---|
| --config | 无 | TOML文件路径 |
| --provider | SCOUT_PROVIDER | 配置中只有一个时自动选中 |
| --endpoint | SCOUT_ENDPOINT | 多入口必须选ID或all |
| --protocol | SCOUT_PROTOCOL | 临时单地址模式必填。支持 `openai_chat`、`anthropic_messages`、`openai_responses` |
| --base-url | SCOUT_BASE_URL | 对应协议的API根地址 |
| --api-key-env | SCOUT_API_KEY_ENV | 存放Key的环境变量名称，不是Key值 |
| --api-key（建议优先交互隐藏输入） | SCOUT_API_KEY | 明文参数可能进入shell历史，慎用 |
| --proxy | SCOUT_PROXY | HTTP/HTTPS/SOCKS5/SOCKS5h URL |
| --proxy-env | SCOUT_PROXY_ENV | 存放代理URL的变量名 |
| --ca-bundle | SCOUT_CA_BUNDLE | 自定义受信任CA文件 |
| --profile | SCOUT_PROFILE | 支持 `baseline` 和 `chat-basic`。Responses 默认 `chat-basic`，其余默认 `baseline` |
| --capability | SCOUT_CAPABILITY | `text`（默认）、`stream`（SSE）、`structured`（JSON Schema）或 `tool`（工具调用） |
| --max-tokens | SCOUT_MAX_TOKENS | 正整数，覆盖profile的1/16默认值；不自动增加 |
| --concurrency | SCOUT_CONCURRENCY | 10 |
| --timeout | SCOUT_TIMEOUT | 30秒，一次HTTP完整请求 |
| --retries | SCOUT_RETRIES | 2，额外重试次数 |
| --max-requests | SCOUT_MAX_REQUESTS | 1000，Provider总请求预算 |
| --max-models | SCOUT_MAX_MODELS | 1000，验证目标上限及单入口发现保留上限 |
| --max-pages | SCOUT_MAX_PAGES | 100，每入口发现上限 |
| --rate-limit | SCOUT_RATE_LIMIT | 未设置；每秒请求启动数 |
| --scan-timeout | SCOUT_SCAN_TIMEOUT | 未设置；整个扫描截止时间 |
| --model（可重复） | 无 | 显式模型ID，跳过发现 |
| --models-file | SCOUT_MODELS_FILE | UTF-8，每行一个ID，跳过发现 |
| --include / --exclude | 无 | 验证前模型ID正则过滤；不是发现接口过滤 |
| --out | 无 | JSON 或 CSV 文件路径。stdout 默认输出完整 JSON |
| --format | 无 | json（默认）或csv；仅选择--out格式，不根据扩展名推断 |
| --overwrite | 无 | false；显式替换现有文件 |
| --verbose | 无 | false。stderr 输出发现候选数和验证计划进度 |
| --pretty | 无 | false。启用后 stdout 输出彩色表格汇总，使用 `--out` 可另存报告 |
| --fail-on-unavailable | 无 | false；完整扫描有失败时退出2 |

不是所有CLI选项都支持SCOUT_*环境变量，请以表格为准。

CSV仅支持scan/verify且必须提供--out；models --format csv、缺失--out或未知格式在任何网络请求前报错。format是CLI输出参数，不从TOML或环境变量读取。JSON默认行为和退出码不变。

同名设置优先级：CLI > 环境变量 > 对应TOML字段 > 默认。Key引用与明文Key、proxy与proxy_env属于不同来源字段，同时出现会冲突，不进行隐式选择。使用多入口all时禁止全局覆盖base_url/protocol/api_key/api_key_env，避免意外向其他入口发送凭据。

## TOML结构

参考[完整示例](../../examples/provider.toml)。当前支持的主要字段：

- `[[providers]]`：name；concurrency、timeout、retries、max_requests、max_models、max_pages、rate_limit、scan_timeout；proxy或proxy_env作为入口默认值。
- `[[providers.endpoints]]`：id、protocol、base_url、api_key_env（推荐）或api_key（不推荐）；proxy/proxy_env覆盖；auth；api_version；models或models_file；profile；capability；max_tokens；ca_bundle。
- auth：default / bearer / x-api-key。default按协议选择，不发送多套认证头。
- api_version：默认2023-06-01，用于Anthropic版本头。
- 当前超时/重试等ScanPolicy字段在Provider层设置；放在Endpoint层不会成为独立策略，不要依赖这种配置。

TOML models_file相对配置文件所在目录；CLI/SCOUT_MODELS_FILE相对当前工作目录。当前ca_bundle统一相对配置文件所在目录（没有配置文件时相对当前工作目录），即使由CLI覆盖也遵循此规则，使用绝对路径可避免歧义。

## URL规则

| 输入 | 规范化根路径 |
|---|---|
| https://provider.example | https://provider.example/v1 |
| https://provider.example/v1/ | https://provider.example/v1 |
| https://provider.example/custom/v1 | 原路径保持不变 |

`openai_chat`、`anthropic_messages` 和 `openai_responses` 分别在根路径后追加 `chat/completions`、`messages` 和 `responses`。发现追加 `models`。Base URL 禁止包含用户名、密码、查询参数或片段。服务商变更路径时应更新配置，HTTP 重定向不会被跟随。

命名profile决定提示词和temperature等基线；max_tokens可由CLI > SCOUT_MAX_TOKENS > Endpoint TOML max_tokens > profile默认值覆盖。只接受正整数（不接受小数、布尔值、NaN或无穷大），不自动修改token字段或扩大预算。多个入口使用全局max_tokens时会显式覆盖所有选中入口；需要不同上限时分别写入Endpoint配置。报告中的probe.max_tokens保存实际生效值，probe_id仍是所选基线，因此比较报告时应一并比较probe配置。

当前不支持任意temperature或max_completion_tokens等token字段切换。增大token上限可能增加费用，也不保证输出文本；请求次数预算仍由max_requests单独控制。

## Responses 非流式文本探测

```powershell
.venv/Scripts/python -m llm_model_scout verify --protocol openai_responses --base-url https://api.openai.com/v1 --api-key-env VENDOR_KEY --model YOUR_MODEL_ID --max-tokens 256 --retries 0 --max-requests 1
```

TOML 中为 Responses 建立独立入口。即使 Chat 和 Responses 使用同一根地址和模型 ID，两者也分别发请求、计数和记录。服务商没有实现 `GET /models` 时，可以显式指定模型跳过发现。[TOML 示例](../../examples/provider.toml)

Responses 将配置中的 `max_tokens` 发送为 `max_output_tokens`。CLI/TOML 默认 16，托管入口默认 256。OpenAI 官方接口要求至少 16，且推理 Token 也占用这一上限。显式 `baseline` 默认使用 1，连接官方接口时需覆盖为至少 16。`chat-basic` 省略 `temperature`，适用于不接受此字段的模型。[OpenAI Responses API 参考](https://developers.openai.com/api/reference/resources/responses/methods/create)

## 流式文本探测

```powershell
.venv/Scripts/python -m llm_model_scout verify --protocol openai_responses --base-url https://api.openai.com/v1 --api-key-env VENDOR_KEY --model YOUR_MODEL_ID --capability stream --max-tokens 256 --retries 0 --max-requests 1
```

三种协议均可选择流式文本。也可在 `[[providers.endpoints]]` 中写入 `capability = "stream"`。用 `--endpoint all` 扫描时，若各入口默认能力不同，可用 `--capability` 统一本次任务。模型发现沿用原有列表接口，显式模型可跳过发现。流式正文默认读取上限为 2 MiB。[协议判据](../design/architecture.md#t09-流式文本探测)

## 结构化输出探测

```powershell
.venv/Scripts/python -m llm_model_scout verify --protocol openai_chat --base-url https://provider.example/v1 --api-key-env VENDOR_KEY --model YOUR_MODEL_ID --capability structured --retries 0 --max-requests 1
```

也可在入口 TOML 中设 `capability = "structured"`。未指定 profile 和 Token 上限的 CLI/TOML 结构化配置默认使用 `chat-basic` 和 128 Token。显式设置按原值使用。Agent 修改本次能力时沿用保存的探测参数，托管连接默认 256 Token。若旧 TOML 注册表显示 Token 上限为 1，可为本次指定 `max_tokens` 或在入口配置结构化能力。[请求判据](../design/architecture.md#t10-结构化输出探测)

## 工具调用探测

```powershell
.venv/Scripts/python -m llm_model_scout verify --protocol openai_chat --base-url https://provider.example/v1 --api-key-env VENDOR_KEY --model YOUR_MODEL_ID --capability tool --retries 0 --max-requests 1
```

也可在入口 TOML 中设 `capability = "tool"`。CLI/TOML 默认 `chat-basic` 和 128 Token，前提是未显式选择 profile 或 Token 上限。托管连接默认 256 Token。每次探测校验模型是否发出合格的合成工具指令，收到指令后即完成验证。[各协议的工具请求与完成判据](../design/architecture.md#t11-工具调用探测)
