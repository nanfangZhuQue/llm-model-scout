# 开发与验证指南

适用版本：0.6.1 · 更新日期：2026-10-01

## 环境

Python3.11+，src-layout，可编辑安装；本地当前使用Windows Python3.12.8。以下命令在项目根目录运行：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m llm_model_scout --help
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m pytest tests/unit/test_classify.py::test_401_is_unauthorized -q
.venv/Scripts/python -m pytest tests/integration/test_socks.py -q
.venv/Scripts/python -m pytest tests/e2e -q
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python scripts/check_docs.py
.venv/Scripts/python -m build
.venv/Scripts/python examples/demo.py
.venv/Scripts/python examples/agent_demo.py
```

macOS/Linux将Python路径改为`.venv/bin/python`。文档检查仅检查仓库内链接/必需文件，不访问外网，不读取密钥配置，也不能证明文字语义与业务一致。

## 测试层次

- unit：配置优先级、URL、协议解析、错误分类、脱敏、分发白名单等。
- integration：HTTPX MockTransport与实际本地HTTP/SOCKS/HTTPS，覆盖限流、重试、预算、取消及代理无回退。
- e2e：真实CLI子进程，以及真实Uvicorn Agent创建/查询/取消/下载；不等同于只测ASGI内存适配。
- Agent专项：Host/Origin/token、受限请求体与输入、共享预算/冷却、历史淘汰、关闭清理及token文件独占创建。
- docs：检查文档入口、链接及hook输出。新增文档不得依赖临时本地绝对路径。

自动测试只使用本地mock。真实服务商验证是独立授权行为，不能作为测试套件默认操作；结果记录在[服务商验证记录](provider-tests.md)。

## 浏览器验证

原生Python Playwright脚本为scripts/test_web_ui.py，需要可选依赖`.[browser]`与`python -m playwright install chromium`。运行`python scripts/test_web_ui.py`会使用本地合成数据并保存桌面/手机截图，不调用外部服务商。

第二阶段发布候选使用 `python scripts/accept_wheel.py --wheel <候选wheel> --browser --output <新证据文件>`。脚本在仓库外新建虚拟环境并校验导入来源，验收四种探测、三种协议、进程中断后历史恢复，以及 JSON、CSV、XLSX 下载。`--browser` 复验从 wheel 安装后的真实工作台。离线环境可通过 `--wheelhouse` 提供已下载的依赖 wheel。

2026-10-03已补跑原生Playwright，并通过全新wheel安装后的UI验收；此前连接的Playwright工具验证仍保留为历史证据。验收命令为`python scripts/accept_wheel.py --browser --output .scout/acceptance-new.json`，父环境需要Playwright，目标应用会安装在仓库外的新venv，详见[验收记录](acceptance.md)。浏览器场景不计入pytest数量；截图和本地记录排除分发。

## 分发

产物在dist，包含wheel与sdist。sdist显式列出src/tests/examples/scripts及选定docs分类；不包含个人配置、运行报告或历史原文归档。docs/archive/README.md作为归档说明可包含，历史快照仅在开发工作区保留。

更改分发范围时同步[打包测试](../../tests/unit/test_packaging.py)，实际检查压缩包路径。不能因为.gitignore存在就假设Hatch已排除所有私有文件。同一版本号的旧构建不会自动包含后来文档；修改后需要重新构建。

## 代码与文档同步

改动业务后按[维护规则](documentation-policy.md)更新对应需求、教程、设计、进度和变更记录。修改默认值先同步配置表；修改JSON字段先同步报告文档并考虑schema版本。修复测试时不能把预期值改成错误结果来让测试通过。

[CI配置](../../.github/workflows/tests.yml)覆盖Windows/macOS/Linux及Python3.11/3.12。尚未远程执行的矩阵必须记为“待验证”，不能根据本地通过宣称跨平台通过。
