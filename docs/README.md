# 项目文档中心

适用版本：0.6.1 · 整理日期：2026-10-01

## 按任务查找

| 想做什么 | 文档 |
|---|---|
| Windows双击启动、直接填写服务商 | [无配置快速使用](user-guide/quick-start.md) |
| 使用浏览器工作台 | [同源Web UI教程](user-guide/web-ui.md) |
| 使用本地HTTP任务服务 | [Agent教程](user-guide/agent.md) |
| 安装并完成第一次扫描 | [用户使用教程](user-guide/getting-started.md) |
| 查看全部参数、环境变量和TOML字段 | [配置与命令参考](user-guide/configuration.md) |
| 理解JSON结果、退出码、失败原因 | [报告与故障排查](user-guide/reports-and-troubleshooting.md) |
| 理解业务范围与验收条件 | [需求说明](requirements/PRD.md) |
| 理解模块边界与各协议的实现 | [技术架构](design/architecture.md) |
| 查看当前验收结论与复现步骤 | [0.5.0验收记录](development/acceptance.md) |
| 看已完成、未完成和验证证据 | [开发进度清单](development/progress.md) |
| 派发第二阶段任务，查看依赖和验收样例 | [第二阶段开发任务](development/phase-2-plan.md) |
| 搭建开发环境和运行测试 | [开发与验证指南](development/development-guide.md) |
| 处理需求新增、改动和文档同步 | [文档维护规则](development/documentation-policy.md) |
| 查看版本与需求变更历史 | [变更记录](development/changelog.md) |
| 查阅原任务书及早期方案 | [历史归档](archive/README.md) |

## 文档边界

- requirements：为什么做、必须满足什么；不把设想写成已实现。
- design：当前技术实现、关键取舍与扩展边界。
- user-guide：用户可直接执行的操作、配置和排障。
- development：交付进度、验证结果、变更与维护流程。
- archive：保留历史原文，不再作为当前实现依据。

根目录README是项目入口，CLAUDE.md是编码助手操作指南；task_plan.md、findings.md、progress.md仅保留规划工具兼容入口，正文集中在docs。配置示例留在examples，脚本留在scripts。RAEMD.md按通常拼写统一为README.md，不维护两份同义入口。

## 信息优先级

新确认的业务要求先进入需求文档和变更记录；实现尚未完成时同步标为“待开发”，不能用旧代码覆盖新要求。描述当前能力时以已验证代码为准，有差异就记录并修正文档。历史归档只用于追溯，不覆盖现行需求。
