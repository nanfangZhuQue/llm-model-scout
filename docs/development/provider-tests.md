# 服务商验证记录

更新日期：2026-10-01。本页仅保存不含凭据的观察结果；不保存API Key、认证头或完整响应正文。需要再次测试时重新从运行环境获取凭据。

## OpenRouter：一次发现与一次聊天验证

- 用户授权：本轮明确提供服务商进行测试；范围限定为一次发现与一个发现到的`:free`模型，不全量扫描、不重试。
- Base URL：https://openrouter.ai/api/v1
- 协议：openai_chat；未测试Anthropic Messages。
- 网络：未配置SOCKS5，HTTPX不继承环境HTTP_PROXY；此项不是代理实测。
- 发现：HTTP200，返回464个模型ID；列表成功不能独立证明Key有效或所有模型可用。
- 验证模型：`cohere/north-mini-code:free`，由发现列表选取，不是预设猜测。
- profile：chat-basic，ping、max_tokens=16、省略temperature、非流式。
- 请求上限：一次发现、一次验证；retries=0，无自动参数或模型回退。
- 验证时间（工具UTC）：2026-09-30T17:00:22.808525+00:00。
- 观察：HTTP200，未通过非空assistant文本校验。
- 工具结果：status=unavailable，error_category=BAD_RESPONSE，reason=probe_failed。
- 延迟：约1657ms，attempts=1；不是性能基准。

### 结论边界

只证明本次探测未通过。没有保存完整响应，因此不能从现有证据断言究竟是token预算、返回格式还是其他兼容因素导致；也不能声称模型永久不可用。工具未进行更高token预算复测，未验证实际计费金额。模型ID带`:free`是选择依据，不是费用审计。

密钥已出现在聊天中，建议用户轮换；文档、报告与示例不保留该密钥。

## 0.3.0 CSV真实导出验收（2026-10-01）

- 授权：用户已明确允许后续OpenRouter自测，无需逐次确认Token费用；本轮按导出验证需要调用一次发现、一次聊天，没有重试。
- Base URL：https://openrouter.ai/api/v1；协议openai_chat；未配置SOCKS5。
- 发现：HTTP200，464个model ID。
- 模型：从当前列表选取openai/gpt-4.1-mini；profile=chat-basic，显式max_tokens=256，temperature不发送。
- 验证时间：2026-10-01T00:28:00.066390+00:00（UTC）。
- 实际结果：HTTP403，FORBIDDEN；脱敏错误为“This model is not available in your region.”；attempts=1，约980ms。
- CSV文件：开发工作区report-openrouter-030.csv；固定列、256预算、403/失败状态及错误消息与stdout JSON一致。此文件不纳入源码分发。
- 结论：真实失败报告导出流程得到验证；不能据此宣称模型可用，也不能证明其他模型或地区同样受限。未绕过地区限制、未自动更换网络路径。没有保存Key或响应正文。
- 旧免费模型的BAD_RESPONSE记录保留；本次不是对旧模型的复测，不能替它确定根因。

## 0.4.0 Agent真实链路验收（2026-10-01）

- 通过真实本地Agent进程调用POST /scans、查询任务并下载JSON/CSV；不是CLI直接访问上游的替代演示。沿用已授权OpenRouter，无代理，不保存Key或会话token。
- 配置：examples/openrouter.toml；显式指定两个历史已发现model ID，profile=chat-basic、max_tokens=256、retries=0；本轮没有重新拉取列表，实际发起两次验证。
- 任务时间UTC：2026-10-01T14:15:56.447738+00:00至14:16:02.887247+00:00；任务completed。
- cohere/north-mini-code:free：HTTP200，存在有效文本，available/OK，stop_reason=length，attempts=1，约6369ms。
- openai/gpt-4.1-mini：HTTP403/FORBIDDEN，错误为当前地区不可用，attempts=1，约1631ms；没有绕过限制。
- 汇总：2目标，1通过/1失败；JSON与CSV下载成功，CSV BOM验证通过。演示结束已清理本次Agent进程，环境凭据随后移除。
- 边界：较高预算下旧免费模型这次通过，不足以证明此前16-token失败的唯一原因；时间与上游状态也不同。length停止并不表示完整回答已生成，只满足本次非空文本可用性判定。未验证Anthropic真实入口或长期可用性。

## 后续测试模板

记录日期/UTC时间、授权与请求上限、协议和Base URL、候选来源、model ID、profile、网络路径、HTTP状态、工具分类、attempts及结论边界。不复制命令中任何凭据，不把失败改写为成功，也不通过反复尝试扩大用户授权范围。
