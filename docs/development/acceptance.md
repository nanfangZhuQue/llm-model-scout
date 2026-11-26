# 0.5.0 验收记录

验收日期：2026-10-03 · 应用版本：0.5.0

## 结论

约定的CLI、Agent、同源Web UI功能已实现；Windows本机验收已覆盖源码运行和全新环境wheel安装。跨平台实际CI尚未执行，因此不能标记“全部平台验收通过”。本轮没有新增业务功能，也没有发起真实服务商请求。

## 验收矩阵

| 项目 | 状态 | 证据与边界 |
|---|---|---|
| Python功能/接口回归 | 通过 | 本轮192项pytest通过（19.29秒），Ruff和文档链接检查通过；与浏览器场景分开计数 |
| 原生Python Playwright | 通过 | 全局Python3.12.8、Playwright1.62.0、Chromium151.0.7922.34 |
| CSP安全配置 | 保持开启 | 修正的是测试脚本等待方式，没有添加unsafe-eval或bypass_csp |
| 全新虚拟环境wheel安装 | 通过 | 仓库外临时目录，未继承系统包，子进程使用python -I |
| 安装来源校验 | 通过 | llm_model_scout来自新venv/Lib/site-packages，不是src或开发环境 |
| wheel中的CLI与Agent启动 | 通过 | 版本0.5.0，HTTP鉴权及UI资源/CSP通过 |
| 双协议扫描与JSON/CSV | 通过 | 4个mock目标，2通过/2模拟不存在；CSV BOM及4行结果通过 |
| wheel中的原生浏览器UI | 通过 | 登录/错误Token、创建、筛选、下载、恶意文本、刷新重登、取消、移动布局与截图 |
| Windows/macOS/Linux CI矩阵 | 待执行 | 当前仅有Windows3.12本机结果，不等于六个CI组合均通过 |

## 可追溯产物

本次被验收的wheel：`dist/llm_model_scout-0.5.0-py3-none-any.whl`。

SHA-256：

```text
47d88947a530c6866ea9ca5feedd4903355db0eb7a544e7a67d9a4b52a1c106c
```

本机机器可读记录：`.scout/acceptance-wheel.json`，包含版本、平台、导入来源、wheel摘要和通过的检查项。临时venv及Agent进程在验收结束后清理，因此记录中的临时安装路径不是永久安装目录。截图位于`.scout/acceptance-browser/`和`.scout/wheel-browser/`，均不纳入分发。

## 本轮修正

原生脚本最初真正执行时，字符串形式wait_for_function触发严格CSP的unsafe-eval限制。改用Playwright元素断言等待，再次运行通过。应用CSP和探测判定不变。

此前项目venv的Playwright下载超时；本轮发现全局Python已有可用Playwright/Chromium，用临时PYTHONPATH使源码测试子进程获得项目依赖，执行完即恢复。干净wheel验收独立进行：子进程清除PYTHONPATH/PYTHONHOME/PYTHONUSERBASE、使用-I，并核实模块来自新环境，不能把两种测试混为同一环境。

## 复现命令

在已安装项目开发依赖的环境中运行：

```powershell
python -m build
python scripts/accept_wheel.py --output .scout/acceptance-wheel-new.json
```

安装可选浏览器依赖及Chromium后，可以同时验证安装后的UI：

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
python scripts/test_web_ui.py
python scripts/accept_wheel.py --browser --output .scout/acceptance-wheel-browser-new.json
```

--output必须是新文件，避免覆盖旧验收证据。脚本在临时环境安装wheel的agent依赖，可能需要联网下载Python包；不会访问真实模型服务商。默认读取pyproject版本选择wheel，不能用旧版本产物冒充当前验收。

## 尚缺的外部条件

- 当前工作目录不是Git仓库，没有可用远程仓库上下文；未发现gh命令。
- WSL查询未列出可执行发行版，本机没有已确认的Linux/macOS测试环境。
- 已补充workflow_dispatch、六组合矩阵的隔离wheel测试，以及Ubuntu/Python3.12的原生浏览器验收步骤，但没有触发远程运行。

下一步需要提供已有GitHub仓库与明确的推送/触发授权，或提供Linux/macOS执行环境。不要为“完成验收”擅自新建公开仓库、上传项目或伪造CI结果。
