# v2.1.4 文档索引

文档同步日期：2026-10-14。当前源码版本为v2.1.4；最近发布为 [v2.1.4](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.4)；正文描述本项目实现，历史日志结论保留原核查日期。

## 按任务查找

| 需求 | 文档 |
|---|---|
| 下载、功能总览、源码/Docker运行 | [项目首页](../README.md) |
| Key教程、平台登录、完成首启配置 | [上手引导](onboarding.md) |
| QQ快速登录、网关推送、Windows回调、镜像与分区保存 | [NapCat](napcat-fixes.md) |
| 图片、连续输入、明确等待、输入提示 | [识图与轮流说话](image-turn-taking.md) |
| 微信输入状态与发送/模型网络故障 | [iLink投递核查](ilink-delivery-check.md) |
| 长期记忆、身份关联、历史/过期事实、摘要 | [上下文与记忆](memory-design.md) |
| 联网搜索、工具参数、时间与时区 | [搜索与时间](web-search-time.md) |
| 高频词/话题、气泡图、计数和扫描限额 | [对话统计](conversation-statistics.md) |
| 自选旧目录、整套导入、预览、重启与恢复 | [旧目录导入（v2.1.4）](data-import.md) |
| 数据目录、非覆盖迁移、备份/恢复、Docker卷 | [用户数据](user-data-persistence.md) |
| 原生构建、资源布局、托盘、退出、故障检查 | [桌面开发](../desktop/README.md) |
| Windows v2.1.3改进与原生验收 | [Windows专项](windows-v2.1.3.md) |
| 发布验收与测试边界 | [验证矩阵](validation.md) |
| 精确发布大小、SHA256、版本与验收快照 | [当前发布清单](release-v2.1.4.json)、[v2.1.3历史清单](release-v2.1.3.json) |
| 版本历史 | [CHANGELOG](../CHANGELOG.md) |
| 弗洛洛角色卡与合成资料 | [人格卡](../personas/phrolova/PERSONA.md)、[评估与迭代](../personas/phrolova/EVALUATION.md) |

## 文档一致性

仓库根目录运行 `python tools/check_docs.py`，检查版本、下载表、清单、代码默认值、页面入口、桌面布局、当前目录、旧说法与本地链接；不抓取所有外链、不调用账号或模型。发布附件摘要另需与 GitHub Release 核验。

v2.1.3 的发布运行时代码固定于标签；`main` 的文档可在此后修正。已发布文件不会因为文档提交自动改变。截图/合成接口/源码回归和真实安装生命周期属于不同验证层，不互相替代。
