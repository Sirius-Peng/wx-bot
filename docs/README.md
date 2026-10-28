# WxBot 文档索引

文档同步日期：2026-10-28。当前源码v3.0.3，正在准备原生QQ网关；当前发布[v3.0.2](https://github.com/Sirius-Peng/wx-bot/releases/tag/v3.0.2)，平台及摘要见[本次清单](release-v3.0.2.json)；[v3.0.0历史清单](release-v3.0.0.json)保留原验收。历史v2验收不挪用为v3结论。

## v3优先阅读

- [内置NapCat与外部OneBot](v3/native-napcat.md)：v3.0.3开发中的Windows x64内置运行环境、明确模式、快速登录、旧数据与macOS外部接入；[发布准备](release-v3.0.3.json)。

- [微信角色语音](v3/wechat-voice.md)：Fish独立Key、显式引擎、页内试听与文字先行的WAV附件；[语音资源来源](v3/voice-sources.md)区分在线音色、语料和本地权重。
- [历史消息日期](v3/history-time.md)：保留原发言时点，分开记录收发时间，不以当前日期补齐旧内容。

- [DeepSeek 1017联网工具升级](v3/search-1017.md)：默认直接Search / Fetch、按需正文、合计两次预算、按次计费与旧native兼容；主模型/Key和路径不变。

- [轻量内核与四页面使用说明](v3/README.md)：快记忆3/7/30天、2/4/6秒收集、独立桌面文字伙伴、上下文和路径兼容。
- [人格与关系的独立记忆空间](v3/persona-spaces.md)：2026-10-20 新增；切换隔离事实／上下文、独立人格快照、全局身份与遗忘、旧文件路径兼容。
- [开发验收与待完成门槛](v3/validation.md)：轻量云判定、会话前缀和真实缓存用量、原生/升级/安装包须分别验收。
- [接话策略与已退役本地实验](v3/local-decision.md)：规则保护、单请求云判定；本地训练工具与失败结果只保留研究用途。
- [稳定会话与KV缓存](v3/context-session.md)：窗口自动/手填、默认40%后压缩、稳定追加前缀与usage真实命中统计。
- [费用核查与减少无效调用](v3/cache-cost.md)：固定v2.1.7源码证据、v3批次判定预算/搜索复用/按需复核，以及手动真实用量诊断；混合token总量不反推缓存率。
- [第三方来源与发行组件声明](v3/third-party.md)：历史模型研究、实际运行依赖、Live2D及机制参考的不同来源与条款。

以下专题中标明v2.1.7的界面、截图或历史测试只适用于旧发布；当前源码入口以四页面说明为准。

## 按任务查找

| 需求 | 文档 |
|---|---|
| 下载、功能总览、源码/Docker运行 | [项目首页](../README.md) |
| Key教程、平台登录、完成首启配置 | [上手引导](onboarding.md) |
| QQ快速登录、Windows内置网关、外部OneBot、旧镜像兼容与分区保存 | [NapCat](napcat-fixes.md) |
| 图片、连续输入、明确等待、输入提示 | [识图与轮流说话](image-turn-taking.md) |
| 微信角色语音、试听、音色与发送形式 | [语音功能](v3/wechat-voice.md)、[来源核对](v3/voice-sources.md) |
| 旧消息相对日期、历史时间与摘要来源 | [历史时间](v3/history-time.md) |
| 微信输入状态与发送/模型网络故障 | [iLink投递核查](ilink-delivery-check.md) |
| 长期记忆、身份关联、历史/过期事实、摘要 | [上下文与记忆](memory-design.md) |
| v2.1.7旧版拓扑说明（v3已移除完整界面） | [历史记忆拓扑](memory-topology.md) |
| 联网搜索、直接Search / Fetch、费用、时间与时区 | [1017工具升级](v3/search-1017.md)、[搜索与时间](web-search-time.md) |
| 低缓存/调用数、近期真实usage与账单边界 | [费用核查](v3/cache-cost.md)、[稳定会话](v3/context-session.md) |
| 高频词/话题、气泡图、计数和扫描限额 | [对话统计](conversation-statistics.md) |
| 自选旧目录、整套导入、预览、重启与恢复 | [旧目录导入（v2.1.4）](data-import.md) |
| 数据目录、非覆盖迁移、备份/恢复、Docker卷 | [用户数据](user-data-persistence.md) |
| 原生构建、资源布局、托盘、退出、故障检查 | [桌面开发](../desktop/README.md) |
| Windows v2.1.3改进与原生验收 | [Windows专项](windows-v2.1.3.md) |
| v3验收与历史发布边界 | [v3状态](v3/validation.md)、[历史验证矩阵](validation.md) |
| 精确发布大小、SHA256、版本与验收快照 | [当前发布清单](release-v3.0.2.json)、[v3.0.0历史清单](release-v3.0.0.json)、[v2.1.7历史清单](release-v2.1.7.json)、[v2.1.6历史清单](release-v2.1.6.json)、[v2.1.5历史清单](release-v2.1.5.json)、[v2.1.4历史清单](release-v2.1.4.json)、[v2.1.3历史清单](release-v2.1.3.json) |
| 版本历史 | [CHANGELOG](../CHANGELOG.md) |
| 弗洛洛角色卡与合成资料 | [人格卡](../personas/phrolova/PERSONA.md)、[评估与迭代](../personas/phrolova/EVALUATION.md) |

## 文档一致性

仓库根目录运行 `python tools/check_docs.py`，检查版本、下载表、清单、代码默认值、页面入口、桌面布局、当前目录、旧说法与本地链接；不抓取所有外链、不调用账号或模型。发布附件摘要另需与 GitHub Release 核验。

各历史版本的发布运行时代码固定于对应标签；`main` 的文档可在此后修正。已发布文件不会因为文档提交自动改变。截图/合成接口/源码回归和真实安装生命周期属于不同验证层，不互相替代。

## 主动联系

- [主动联系（单端/双端）](proactive-messages.md)：仅微信或仅QQ直接使用、可选身份关联、共享时间线、免打扰、失败与本地数据。

## 作者与参与

- [作者与贡献者](../CONTRIBUTORS.md) · [贡献指南](../CONTRIBUTING.md)
