# 用户使用教程

适用版本：0.6.1 · 更新日期：2026-10-01

所有命令从项目根目录执行。真实API请求可能收费；先用本地演示确认工具可运行，再用少量明确授权的模型测试。不要把真实密钥写进命令参数、文档或报告。

推荐直接使用[Windows无配置快速使用](quick-start.md)。以下是保留的手动CLI/配置文件流程，不再是使用工具的前提。

## 1. 安装

需要Python 3.11或更高版本。

Windows PowerShell：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e .
.venv/Scripts/python -m llm_model_scout --help
```

macOS/Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m llm_model_scout --help
```

后面的示例使用Windows路径；macOS/Linux替换为`.venv/bin/python`。激活环境后，可用`model-scout`代替`python -m llm_model_scout`，无需激活也能使用完整路径。

数据目录：默认 `%LOCALAPPDATA%\ModelScout\`（Windows）或 `~/.local/share/ModelScout/`（Linux/macOS）。指定 `--data-dir <路径>` 或 `--local-data`（在项目根目录创建 `data/`）可自定义本机加密数据库位置。

## 2. 先运行不收费演示

```powershell
.venv/Scripts/python examples/demo.py
```

脚本自动启动和关闭本地mock，依次运行models、Anthropic verify和双入口scan。预期最终扫描四个合成目标，2通过、2模拟模型不存在，共6次请求。它不连接外部服务商。

保存一份演示报告（文件应尚不存在）：

```powershell
.venv/Scripts/python examples/demo.py --out report-local.json
```

## 3. 准备服务商凭据

收集服务商提供的OpenAI/Anthropic协议地址、API Key以及候选model ID。协议地址可能相同，也可能不同；不要根据模型名称决定协议。

在你自己的交互式PowerShell中输入密钥，避免将字面密钥保留在命令历史中：

```powershell
$secret = Read-Host 'API Key' -AsSecureString
$env:VENDOR_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
Remove-Variable secret
```

环境变量仍是进程内的明文，使用完执行`Remove-Item Env:VENDOR_KEY`。不要执行打印该变量的命令，也不要提交包含密钥的配置。自动化环境可由受信任的密钥管理系统注入同名变量。

## 4. 单入口：先发现，再单模型验证

下面的provider.example和YOUR_MODEL_ID是占位值，必须替换；不要直接复制它们去验证真实服务。

```powershell
.venv/Scripts/python -m llm_model_scout models --protocol openai_chat --base-url https://provider.example/v1 --api-key-env VENDOR_KEY --max-requests 3
.venv/Scripts/python -m llm_model_scout verify --protocol openai_chat --base-url https://provider.example/v1 --api-key-env VENDOR_KEY --model YOUR_MODEL_ID --retries 0
```

使用 Anthropic Messages 时将 `--protocol` 改成 `anthropic_messages`。使用 Responses 时改成 `openai_responses`。Base URL 填写对应 API 根地址，例如 `https://provider.example/v1`。[协议与配置参考](configuration.md)

baseline默认只允许1 token且发temperature=0，某些模型可能空输出或拒绝参数。需要复测时，显式使用`--profile chat-basic`（16 tokens、省略temperature），而不是直接认定模型不可用。该配置也不保证适配所有模型。

0.2.0起可显式覆盖token预算，例如：

```powershell
.venv/Scripts/python -m llm_model_scout verify --protocol openai_chat --base-url https://provider.example/v1 --api-key-env VENDOR_KEY --model YOUR_MODEL_ID --profile chat-basic --max-tokens 128 --max-requests 1 --retries 0
```

该命令使用占位地址和模型，必须替换后才能真实运行；增加上限可能增加费用。profile仍决定其余参数，报告probe记录实际max_tokens。先查看diagnostic_code：只有明确长度终止才建议考虑增大预算，empty_text不能证明预算不足，refusal不应自动重试。

## 5. 多入口配置

复制配置示例并另存为 `providers.local.toml`。示例启用了 Chat 和 Messages，可取消注释以添加 Responses。各入口可引用同一个环境变量或不同变量，实际 Key 存在环境变量中。[配置示例](../../examples/provider.toml)

```powershell
.venv/Scripts/python -m llm_model_scout scan --config providers.local.toml --provider vendor-a --endpoint openai --max-requests 20 --out report.json
.venv/Scripts/python -m llm_model_scout scan --config providers.local.toml --provider vendor-a --endpoint all --max-requests 40 --scan-timeout 120 --out report-both.json
```

`all` 明确开启全部已配置入口。入口之间共享并发和请求预算，同名模型各自在所属入口产生结果。

## 6. 模型列表不可用时

建立UTF-8文件`models.local.txt`，每行一个真实model ID；空行会忽略，重复ID去重，大小写保留。不要加入注释行，因为注释也会被当作模型ID。

```powershell
.venv/Scripts/python -m llm_model_scout scan --protocol anthropic_messages --base-url https://provider.example/v1 --api-key-env VENDOR_KEY --models-file models.local.txt --max-requests 10
```

也可重复使用`--model MODEL_A --model MODEL_B`。手动模式不调用models端点。两种手动来源不能同时提供。

## 7. 使用SOCKS5

无鉴权代理示例：

```powershell
.venv/Scripts/python -m llm_model_scout scan --config providers.local.toml --provider vendor-a --endpoint all --proxy socks5://127.0.0.1:1080 --max-requests 10
```

有用户名/密码时，通过环境变量提供完整代理URL，再用`--proxy-env VENDOR_PROXY`引用；URL中的特殊字符需要百分号编码。不要同时配置proxy和proxy_env。工具不自动继承HTTP_PROXY/HTTPS_PROXY，也不在SOCKS失败后直连。

## 8. 控制成本与保存结果

- `--retries 0`关闭重试；默认2代表最多三次尝试。
- `--max-requests`计入模型发现页、验证及重试；不等于模型数量。
- `--max-models`限制探测目标数量；`--rate-limit`限制每秒请求启动数。
- `--scan-timeout`给整个扫描设置截止时间。
- `--verbose` 将累计候选数和验证进度输出到 stderr。计划确定后显示已验证数和计划目标数，普通报告仍从 stdout 或文件读取。
- `--out`默认保存JSON；`--format csv --out report.csv`导出CSV，stdout仍是JSON；已有文件须加`--overwrite`，父目录须事先存在。
- Ctrl+C会尝试保存部分报告；强杀进程不能保证保存。

## 9. 导出CSV供表格分析

先用本地mock试用（目标文件应不存在）：

```powershell
.venv/Scripts/python examples/demo.py --format csv --out report-demo-csv.csv
```

实际服务商示例：

```powershell
.venv/Scripts/python -m llm_model_scout scan --config providers.local.toml --provider vendor-a --endpoint all --max-requests 20 --format csv --out report.csv
```

CSV仅适用于scan/verify，必须指定--out；models保持JSON。文件使用UTF-8 BOM，包含每个模型的协议、状态、诊断和预算。文本可能为防公式注入添加单引号；精确原值或完整发现信息请看stdout JSON。格式不由文件扩展名自动选择。

下一步阅读[配置参考](configuration.md)、[报告/排障说明](reports-and-troubleshooting.md)；需要HTTP任务接口时使用[Agent教程](agent.md)。
