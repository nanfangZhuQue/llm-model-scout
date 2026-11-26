# LLM Model Scout

本地模型可用性扫描工具，支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages、HTTP/HTTPS/SOCKS5 代理，以及 Web UI 和 CLI。

三种协议均可显式选择非流式文本、流式文本、结构化输出或工具调用探测。[探测配置](docs/user-guide/configuration.md)

## Windows快捷启动

安装Python3.11+后，在完整项目目录中双击：

- **[start-web.bat](start-web.bat)**：启动Web UI并打开浏览器。
- **[start-cli.bat](start-cli.bat)**：打开交互式命令行。

脚本自动准备运行环境。无需编辑配置文件：直接填写服务商名称、Base URL、Key和代理，保存后自动加密保存在本机，下次启动恢复。

Web UI仍需用启动窗口显示的Agent会话Token登录；它不是服务商API Key。参见[无配置快速使用](docs/user-guide/quick-start.md)。

## 使用流程

新增服务商 → 获取模型 → 勾选/批量测试 → 查看正常回复验证结果 → 导出。

普通测试报告始终脱敏。需要备份连接信息时，可明确选择导出包含Key的完整配置；此文件为明文，不要上传云仓库。

模型列表不是可用性证明。结果只对本次凭据、协议、参数、网络和时间成立，不证明模型身份或长期可用性。真实请求可能收费。

## 命令行启动

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[agent]"
.venv/Scripts/python -m llm_model_scout desktop
.venv/Scripts/python -m llm_model_scout interactive
```

旧TOML/环境变量/scan命令保持兼容。macOS/Linux替换Python路径，自动加密保存需要secure-store可选依赖和系统密钥库。

## 文档

- [文档中心](docs/README.md)
- [需求说明](docs/requirements/PRD.md) / [技术架构](docs/design/architecture.md)
- [无配置快速使用](docs/user-guide/quick-start.md) / [参数参考](docs/user-guide/configuration.md)
- [Web UI](docs/user-guide/web-ui.md) / [Agent API](docs/user-guide/agent.md)
- [报告与排障](docs/user-guide/reports-and-troubleshooting.md)
- [开发进度](docs/development/progress.md) / [变更记录](docs/development/changelog.md)

## 当前版本

0.6.1：新增直接管理服务商、自动加密保存、独立模型发现/选择测试、显式完整配置导出、Windows双启动入口及分类.gitignore。

Agent 将脱敏扫描历史保存到本机 `scans.sqlite3`，服务商连接配置单独加密保存。工作台可跨会话分页、按服务商筛选和下载报告。历史默认保留最近结束的 50 个任务，可通过 `--max-history` 调整。[Agent 教程](docs/user-guide/agent.md)

跨平台 CI 与非 Windows 原生密钥库验收仍待验证。
