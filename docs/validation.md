# v2.1.6 验证矩阵

发布日期：2026-10-15（Asia/Shanghai）；构建运行时源码提交 `1eb316fd86039a85599f582da3491f66c592d05c`；[Release](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.6)、[文件清单](release-v2.1.6.json)。发布文档与文件摘要可以晚于构建提交；包内运行源码另行比对。

本次由对应平台原生构建Windows x64 Setup / Portable与macOS arm64 DMG。Windows恢复同步发布，不沿用或重命名历史安装包。

| 层次 | 结果 | 验证范围 |
|---|---|---|
| macOS源码Python | 635项，1项跳过 | 临时目录、模拟HTTP/SDK、原有功能、作者资料、语料导出与资源精简 |
| Windows原生源码Python | 635项，1项跳过 | 独立Python虚拟环境，中文/空格路径，实际Windows文件锁与路径语义 |
| Node回归 | 两端各9组通过 | 前端确认/焦点、引导、统计、导入、主动联系与桌面生命周期合同 |
| macOS冻结后端 | 通过 | 主动联系联系人/可选关联、分区保存；旧资料导入、备份、重启和源资料不变 |
| macOS资源与架构 | 通过 | 14个关键模块字节码、6份Web资源、ASAR主进程源码；全部arm64，6份图标仅保留外部副本 |
| macOS DMG | 通过 | hdiutil验证，只读挂载267项与构建App逐项一致，Applications链接正确 |
| Windows冻结后端 | 通过 | 鉴权、统计/Jieba/时区、备份、记忆持久化、主动联系和旧目录导入/重启 |
| Windows资源与进程 | 通过 | x64 PE、完整onedir树、ASAR版本/作者/主进程、Web资源与14个关键模块；管理器启动/端口回退/重启/退出 |
| Windows便携版 | 通过 | 实际Electron程序启动内置后端，认证及打包页面资源；仅测试进程树清理 |
| 公开附件 | 发布时校验 | 三个原生安装包与SHA256SUMS，附件大小、摘要及下载内容匹配 |

所有过程使用隔离资料目录和模拟接口。Setup本次核验PE与构建资源，未执行完整安装/卸载循环；便携版HTTP启动检查不替代交互式键盘/IME或真实扫码验收。未调用真实账号或付费模型验证供应商识图/搜索质量。

复现入口：`python -m unittest discover -s tests`、全部`tests/*.cjs`、`python tools/check_docs.py`；冻结后端运行 `tools/smoke_proactive.py`、`tools/smoke_data_import.py`，Windows另有`tools/verify_windows_backend.py`和`tools/verify_windows_manager.cjs`。语料导出和构建上下文规则亦有独立回归。

---

# 历史：v2.1.5 验证矩阵

发布日期：2026-10-15（Asia/Shanghai）；运行时代码提交 `c8635ccd69f0ed64bbf270bacc5a595b65aae616`；[Release](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.5)、[文件清单](release-v2.1.5.json)。后续文档提交不改变包内运行时代码。

本次先发布macOS arm64。Windows构建机离线，经确认先发布可用平台；未构建或原生验收Windows v2.1.5，Windows安装下载仍为历史v2.1.4，不把Node协议合同测试当成新的Windows原生验收。

| 层次 | 结果 | 验证范围 |
|---|---|---|
| macOS源码Python | 617项，1项跳过 | 单端/跨端主动联系、搜索总结/复核、多图/视频、原有记忆/投递/导入及平台合同；大小写不敏感文件系统跳过冲突夹具 |
| Node回归 | 9组全部通过 | 主动联系前端11项、输入焦点23项、引导11项、统计18项、导入10项及路径/生命周期合同 |
| macOS原生冻结后端 | 通过 | 双端未关联联系人各自启用、可选关联合并、独立设置保存、状态无凭据泄露、机器人保持停止、无模型调用 |
| 冻结旧目录导入与重启 | 通过 | 旧人格、记忆、微信路径、未知文件、空目录与备份，源资料不变 |
| 包内源码与架构 | 通过 | 14个关键Python模块字节码逐一匹配；全部6份Web资源、ASAR及sidecar匹配，Electron/Framework/Python均arm64 |
| DMG | 通过 | hdiutil校验有效，只读挂载267项与构建App一致，Applications安装链接正确 |

以上测试使用独立临时目录与模拟接口，不使用真实账号或付费模型。识图/搜索的实际供应商质量、平台最终送达/已读与Windows交互式键盘/IME未在本次实测。

复现：`python -m unittest discover -s tests`、`node tests/test_proactive_frontend.cjs`、`python tools/check_docs.py`。冻结主动联系API：`python tools/smoke_proactive.py --backend desktop/resources/backend/wxbot-server`；冻结旧目录导入：`python tools/smoke_data_import.py --backend desktop/resources/backend/wxbot-server`。

---

# 历史：v2.1.4 验证矩阵

发布日期：2026-10-14（Asia/Shanghai）；运行时代码提交 `78c384836d13eff63efe7ea0a7d33ba28e05552a`；[Release](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.4)、[文件清单](release-v2.1.4.json)。后续发布文档提交不改变该运行时代码。

| 层次 | 结果 | 验证范围 |
|---|---|---|
| macOS源码Python / Windows11 x64原生Python | 各533项，1项跳过 | 现有492项及旧目录导入、UTF-8控制文件；默认大小写不敏感文件系统跳过大小写冲突夹具 |
| 生命周期 / 存储路径 / 首启 / 统计 Node | 15 / 12 / 11 / 18项通过 | 延续原有回归，不以旧测试日志替代新源码运行 |
| 旧目录导入前端 | 10项通过 | 预览、确认、选择器、取消、晚到响应、重启重试 |
| 输入焦点 | 23项通过 | 去除原生confirm、独立确认决策、IME事件保护、过期close事件、焦点生命周期、目录选择finally |
| 最终Windows包 | 通过 | 原生x64 PE、ASAR/main/window/preload及全部Web资源匹配、onedir词典/时区与完整树 |
| 最终Portable原生启动与管理器 | 通过 | 实际Electron/冻结后端启动、认证、引导/统计/导入/确认资源、重启与退出 |
| macOS重建 | 通过 | 同一运行时代码重新冻结；Electron/Framework/Python arm64，ASAR/Web/后端匹配 |
| macOS DMG / 冻结导入 | 通过 | 压缩卷校验、只读挂载与App资源一致，旧人格/记忆/路径/未知文件/空目录/备份及重启恢复 |

最终焦点修复版 Setup 未重复完整安装/卸载/重装循环；该生命周期验证来自同日早先 v2.1.4 候选包。Windows交互式键盘/IME和真实微信/QQ账号扫码未实测。浏览器实际输入确认操作后仍正常，Node覆盖IME事件分支；这两项不等同于Windows实机输入质量验收。

复现：`python -m unittest discover -s tests`、`node tests/test_data_import_frontend.cjs`、`node tests/test_input_focus.cjs`、`python tools/check_docs.py`；Mac冻结导入为 `python tools/smoke_data_import.py --backend desktop/resources/backend/wxbot-server`。资源核验与原生Windows入口仍见[桌面说明](../desktop/README.md)。

---

# 历史：v2.1.3 验证矩阵

发布日期：2026-10-14（Asia/Shanghai）。运行时提交 `aba974b5c31f7fa9958830d226848eca5a7c225a`；[公开Release](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.3)。文档提交可以晚于该运行时提交。

| 层次 | 发布时结果 | 能证明什么 |
|---|---|---|
| macOS源码Python回归 | 492/492 | 临时文件、合成消息、模拟SDK/HTTP、认证、持久化、合并/取消、搜索/时间、统计、存储与平台合同 |
| Windows11 x64源码Python回归 | 492/492 | 上述逻辑在Python3.13.9原生Windows通过，含真实文件锁/路径语义 |
| Windows生命周期Node | 15项 | 合并启动/重启、取消、旧事件、超时清理、退出守卫、错误页重试 |
| 存储路径Node | 12项 | 固定平台路径、自定义路径、迁移参数、卸载配置 |
| 首启前端Node | 11项 | 任务单飞、增量保存、二维码实际响应/解码、取消晚到响应 |
| 统计前端Node | 18项 | 气泡面积/无重叠、键盘、文字注入不执行、筛选竞态、空/错误结果 |
| 原生Windows包核验 | 通过 | x64 PE、ASAR版本、完整onedir树及内容、Jieba词典、IANA时区 |
| Setup生命周期 | 通过 | 安装、原生桌面进程/HTTP后端启动、管理器重启/退出、卸载/重装和测试数据marker保留 |
| Portable真实启动 | 通过 | 便携外壳启动内置后端，内置页面、引导/统计资源与认证可用 |
| 冻结iLink/LLM夹具 | 通过 | 真实SDK到本地模拟接口，完整消息、业务拒绝、同草稿/ID/上下文重启恢复；记忆队列、非法JSON、重试、保存及注入 |
| QQ离线链路 | 通过 | 真实HTTP/反向WS到虚拟OneBot与假模型；鉴权、群@、静默、私聊、去重、记忆和停止 |
| macOS arm64包 | 通过 | Electron主程序/Framework/Python均arm64、DMG校验有效 |
| 公开发布 | 4/4摘要匹配 | main发布时与v2.1.3标签指向相同运行时提交；三个二进制及SHA256SUMS大小/摘要与附件匹配 |

## 测试环境与结果解释

Windows 11 Pro x64（10.0.26200）、Python3.13.9 x64、Node24.20.0；Electron36.9.5。Windows构建/运行数据使用中文和空格路径，Setup生命周期使用独立安装目录，不修改用户真实聊天。发布时有针对性地定位了两项Windows基线失败：统计读句柄阻止清空以及首次迁移锁初始化竞争，修复后原生回归全绿。

Setup和Portable是真实进程/API启动验证，不是远端截图或完整视觉UI验收。二维码/快速登录前端检查使用模拟状态；未用真实微信/QQ账号扫码、真实视觉模型质量评估或付费搜索调用替代离线回归。

冻结投递/记忆夹具明确关闭搜索、合并窗口和拟人等待，以隔离投递与积累；搜索、突发合并、自然等待分别由源码测试覆盖。不能据此声称所有功能组合或所有网关/模型版本已端到端实测。

同机3次冻结后端启动中位数：4259.89ms→3384.45ms；本机HTTP工厂暖创建332.08ms→0.09ms。系统缓存、网络和后台负载会影响结果，首个TLS客户端仍需初始化；这些数字不是模型生成或微信客户端送达时间。

## 复现入口

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_windows_lifecycle.cjs
node tests/test_storage_paths.cjs
node tests/test_onboarding_frontend.cjs
node tests/test_statistics_frontend.cjs
python tools/smoke_ilink.py
python -m src.verify_qq
python tools/qr_check.py
python tools/check_docs.py
```

使用项目虚拟环境；Windows原生构建入口是 `cd desktop && npm run pack:win`，其他环境不能代替其PE/安装验证。打包工具自带后端API及管理器检查，见[桌面说明](../desktop/README.md)。

完整发布文件尺寸和SHA256见[release-v2.1.3.json](release-v2.1.3.json)；下载校验可复现，真实账号/供应商行为需单独验证，不把结构测试通过当成人格效果、识图质量或平台客户端已读证明。
