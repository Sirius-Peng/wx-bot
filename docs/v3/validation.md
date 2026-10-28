# v3 验收记录与发布边界

初次记录：2026-10-19；更新：2026-10-28。v3.0.2 本轮构建和验收见[新版发布记录](../release-v3.0.2.json)；以下开发阶段历史逐项保留，v3.0.0 的真实安装包与未执行范围仍以[历史发布清单](../release-v3.0.0.json)为准，历史[v2.1.7](../validation.md)不自动适用于v3。

## 已观察的阶段性与历史结果

2026-10-23策略已切换为轻量云判定与稳定会话前缀。下表已有计数/模型数字保留其原测试范围，**不是本次改动的最终完整回归或当前云API性能**；新的真实API、token及KV命中报告另行补充。

| 层次 | 当前记录 | 证明的范围 |
|---|---|---|
| 重构前源码基线 | Python 755项，跳过1项，通过 | 原HEAD的本地离线回归，为比较/回退基线 |
| 退役前v3源码回归 | Python 1098项，跳过3项，通过；13套Node回归通过；文档一致性检查通过 | 离线回归/合成资料/假时钟/可控模型，不等于真实平台或安装包验收 |
| 人格空间 | 模式/关系隔离、切回恢复人格文字与事实、旧空间队列、提交中切换、全局遗忘、备份导入、重启路径通过 | 详见[独立空间](persona-spaces.md)，原数据根与旧文件名保持 |
| 20组固定时间线 | 全部通过：打断/漏答/重复提交/记忆越界均0 | 机制回归，不作为真实长期陪伴满意度证明 |
| 桌面HTTP/WS集成 | `COMPANION_SMOKE PASS`，模式切换与恢复通过 | 隔离本地后端+确定性LLM桩；流式一个完整气泡、重复client_id不重入、重连只恢复完成稿、异源Origin拒绝 |
| Live2D资源构建与浏览器交互 | `LIVE2D_BUNDLE PASS`；真实模型渲染、中文输入、单气泡通过 | 官方SDK Core/Framework资源及浏览器交互，不等同原生Electron窗口、失焦恢复或用户任意模型兼容 |
| macOS原生开发窗口 | 独立窗口、默认形象、官方Haru导入渲染、中文输入、目录弹窗后焦点、隐藏恢复、正常退出通过 | 隔离资料及本地LLM桩；尚非冻结安装包或完整原生验收 |
| 历史中文模型训练/量化/校准 | 已实际执行；质量失败 | 固定三分类头、per-tensor动态INT8、量化后校准，训练日志与评测产物保留在开发缓存 |
| 历史模型测试 | 1998条合成分家庭测试，准确率77.63%、静默精确率88.21%，保护切片16误静默 | 不满足发行质量门，试验权重不进入发行资源 |
| 历史模型256-token速度 | macOS arm64 CPU、2线程、100次：p50 108.27ms，p95 110.07ms | 仅本机ONNX推理，历史本机结果；Windows另见下行，不是用户端到端时延 |
| 历史模型资源补测 | 含哈希冷加载1284.90ms，进程峰值RSS 3,117,236,224字节，研发目录932,518,474字节 | 峰值含加载/测量，目录大小不等于最终安装包增量；短文本测试p95 13.02ms不能替代256-token结果 |
| 历史Windows继续训练与复测 | CUDA追加2轮完成（267.85秒）；4线程+实际worker性能核策略，CPU 256-token p95=225.70ms（100次） | 本机速度通过；1998条开发回归静默精确率90.82%、保护误静默1/666，质量仍失败，Gather候选另测p95=232.47ms、静默精确率93.03%、保护误静默2/666，质量仍失败；原生149项decision测试通过，详见[Windows训练记录](windows-training.md) |
| 无模型打包策略 | 运行/打包已移除本地判定依赖，新增资源门禁 | 缺模型允许构建；源码策略与实际backend资源分别核验，拒绝旧ONNX/tokenizer/训练依赖；不是已经产出原生安装包 |

阶段性计数须由最终完整回归覆盖。不要把命令运行结束、模型导出成功、包文件存在或一个浏览器截图当作完成全部验收。

## 历史发布前门槛（2026-10-23记录）

本节保留当日计划及勾选原样；本次完成范围以文末发布记录和[本次发布清单](../release-v3.0.0.json)为准，不从发布状态推断未测项目通过。

- [ ] 轻量云判定：覆盖口语/繁体/中英混输/反讽/引用/情绪/撤销/更正/连续补充；保护诉求不误静默，短R/D/C协议、非思考单请求、3秒总期限、失败保守回应；独立配置兼容、并行预判同轮一次复用与新来话取消。
- [ ] 真实API与会话：在Windows主验证机记录判定/主回复端到端延迟、请求数和真实usage token；窗口自动/手填、默认40%后压缩、完整稳定前缀、遗忘/人格/provider变更失效与不跨用户复用。缓存hit/miss仅来自服务返回，缺失为未知，不承诺100%。
- [ ] 更新后的完整Python/Node回归和20组固定时间线（旧版已通过，新策略须重跑）：持续输入、超过50条、生成/限流中追加、明确等待/结束、稍后补回取消/过期、混合图文+视频、搜索总结、单气泡和投递恢复。
- [x] 本地记忆回归：3/7/30天与半衰期假时钟；100次检索/助手提及/重评分不延长期限；摘要、在途整理、旧资料导入/恢复不复活已遗忘内容；人格不被偏好记忆覆盖。
- [x] 覆盖升级合成夹具（不等于原生覆盖安装）：v1.3.6、v2.1.4、v2.1.7夹具，保留原路径、自定义人格、登录、未知文件、记忆、遗忘标记与待投递状态；校验备份与回退。
- [ ] macOS原生Electron：伙伴渲染/拖动/置顶/隐藏/穿透恢复，窗口边界与重启、目录选择/确认后中文IME焦点；不是仅浏览器或Node合同。
- [ ] Windows原生：在远端主验证机验证x64后端、中文/空格路径、所有输入框、目录/扫码后焦点、QQ快速登录、连接状态与实际安装/重装/退出。
- [ ] 媒体与超限：私有sidecar引用/完整像素恢复、原JSON不重复写base64、备份/导入与遗忘；官方Flash内部46MiB安全预算/600图检查，历史必要时压缩，当前超限一次自然分段并保留未读原文；不虚构I/O或API成绩。
- [ ] 不含本地判定模型的原生安装包：确认未加载/预热/下载模型、SDK/许可证与实际资源一致、架构/安装包/覆盖升级/回退校验；无网络时管理界面与数据仍可用，云聊天不可冒充离线可用；正式发布清单和SHA256。

后续应用验收以远端Windows x64为主，macOS按实际产出平台分别验证。已退役本地模型的工具、输入、权重和失败质量结果保留在[研究归档](windows-training.md)，不作为当前发行前提，不重命名为当前云判定成绩。新的性能与消耗只按实际请求和服务usage补录，未实测不预填。

macOS已完成上述原生开发窗口子集，仍需拖动/置顶/穿透恢复、边界/重启和最终安装包验收。未设置新的公开Release，不改名重传旧附件冒充v3。稳定会话/缓存语义及验收要点见[会话与KV缓存](context-session.md)。

## 复现入口

在仓库根目录、安装项目依赖的Python虚拟环境中执行：

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
for suite in tests/*.cjs; do node "$suite" || exit 1; done
node desktop/scripts/verify-runtime-resources.cjs --source-only
.venv/bin/python tools/eval_natural_conversation.py
.venv/bin/python -m tools.smoke_companion_v3
.venv/bin/python tools/check_docs.py
```

Windows使用`.venv\Scripts\python.exe`及PowerShell逐项运行Node套件。训练/评测由源码外独立环境运行，不使用应用用户数据目录；模型实验脚本的静态检查不替代原生PowerShell执行、CUDA训练或CPU测速。集成冒烟默认新建临时用户目录，使用占位Key和本地LLM桩，不调用真实付费模型。`tools/check_docs.py`只核对文档和代码一致性；若其仍报告旧页面/默认值，应更新检查器而不是把新行为改回旧文档。

当前接话策略及历史模型复现见[接话判定](local-decision.md)；真实账号识图、搜索事实质量、平台客户端送达与长期陪伴体验须另行评估，离线桩和源码测试不能代替。

## 2026-10-23：稳定会话前缀专项

- 主回复不再按8轮/1600估算token滚动重建，也不按5,000 token自动摘要；官方已识别DeepSeek模型自动窗口1M，默认40%触发压缩，未知模型自动32K，可手动配置。
- 20轮公开合成对话的真实上下文装配器离线对照：旧版19次续接中完整保留上轮输入前缀 **0/19**，新版 **19/19**，包括已完成助手正文。该实验没有调用API，**不是19次真实KV命中，也不是模型速度测试**。
- 源码回归：Python **1303项、跳过3项**通过；Node **15组**通过；文档及无本地模型资源门禁通过。含1M窗口40%边界、重启、工具来源、群聊遗忘、在途取消、单次超限提示、多图与媒体blob恢复。
- 当前Windows构建机SSH连接超时，尚未补做本次原生安装、真实API缓存率或费用测试。本节不替代Windows/macOS安装包与覆盖升级验收，未发布新版安装包。

## 2026-10-23：调用费用与真实用量专项

- 对照固定v2.1.7 tag确认动态时间/召回插在真实历史之前、未明确禁用思考及搜索固定复核等路径；17M混合token/约30元不足以确定缓存命中率或账单归因。详见[费用核查](cache-cost.md)。
- 当前源码同一连续输入批次最多一次实际云判定，取消/超时后迟到C/D结果不再发布；最新全文保护规则保留，旧静默结论不复用。原生搜索使用非思考、1024输出上限、60秒成功正文缓存与single-flight，复用HTTP池，无关设置保存不重建搜索池。
- 用真实OpenAI SDK和合成HTTP响应验证请求/用量按ID配对；重试、取消与缺失usage保持未知；重复SSE累计usage只结算一次；observer/sink错误不导致重新生成付费回答。原生搜索协议的缓存分量分开记录，不与OpenAI字段混加，思考不重复加到输出。
- 同一合成批次3个快照与两轮deep+tools对照：本轮修改前/后云判定3→1次，工具协议请求3→2次，均投递2个完整气泡。该记录是离线机制回放，不是实测API token或省费比例；回滚复制件应恢复修改前行为，在线工作树保留修复。
- 本轮Python **1432项、跳过3项**通过；Node **16组**通过（含用量前端专项14项）。文档一致性与无本地模型资源检查通过。未调用付费API，不把模拟缓存字段当作真实服务命中；本次未新增原生Windows/macOS安装包验证或发布。


## 2026-10-25：1017直接联网工具

默认配置和旧配置缺字段时使用 `search_backend=tools`，手动可选 `native`。本次配置/API与前端专项使用临时文件、合成Key、本地ASGI及Node沙箱，不调用付费接口；具体工具协议、预算、缓存及集成回归按本轮验证报告记录。直接Search / Fetch按工具请求次数计费，不将其请求数冒充模型token；未返回用量显示未知。主模型名、Key、配置及用户数据路径保持。

10月23日native辅助模型专项是历史记录，不代表当前默认tools仍调用Anthropic模型。本次未宣称实测搜索质量、缓存率、省费比例或端到端时延，未更新v2.1.7已发布附件；详见[1017工具说明](search-1017.md)。

## 2026-10-25：1017直接Search/Fetch

- 当前默认`search_backend=tools`，采用固定官方Search/Fetch接口，不再调用辅助搜索LLM；`native`可手动切回，不自动双路请求。原有Key复用及文件路径保持。
- 新工具保持真实正文/长摘要/片段、来源日期和截断标记，模型工具每轮合计最多两次；原始工具请求单独统计，不混入模型token或KV分母。正常一次整合，只有不合格成稿有界修订。
- 公开文档真实接口观察：Search 481.39ms、Fetch 91.81ms，均HTTP200；两次HTTP、零次LLM，未修改配置或发送聊天数据。这是单次样本，不是总体性能/费用指标；详细机制及范围见[1017联网说明](search-1017.md)。

本轮完整源码回归：Python **1507项、跳过3项**通过，Node **17组**通过（含新搜索后端20项及用量显示15项）。新增38项provider协议回归和24项独立审查覆盖预算、缓存、取消、正文分级、私密URL、重试及计费统计分流。文档与无模型源码资源检查通过。原生安装包验收及Release发布另行执行。

## 2026-10-25：v3.0.0本次发布记录

仅发布清单中的实际平台，本次验证元数据如下；上述开发历史中的“尚未”按其记录日期理解，不作为当前平台矩阵。

```json
{
  "python_macos": 1507,
  "python_macos_skipped": 3,
  "python_windows_native": 1507,
  "python_windows_skipped": 9,
  "python_full_regression_each_platform_passed": true,
  "node_suites_each_platform": 18,
  "windows_flaky_test_fixture_fixed": "Fast image is completed before an artificial deadline; production media timeout unchanged.",
  "windows_backend_runs": 3,
  "windows_x64_frozen_backend_and_onedir_verified": true,
  "windows_setup_pe_verified": true,
  "windows_portable_native_electron_verified": true,
  "windows_packaged_companion_restart_port_change_verified": true,
  "windows_renderer_chinese_input_and_dom_confirmation_focus_verified": true,
  "windows_frozen_legacy_v136_import_preserves_persona_unknown_file_empty_directory": true,
  "cover_upgrade_v136_v214_v217_synthetic_fixtures_passed": true,
  "macos_dmg_arch": "arm64",
  "macos_dmg_integrity_and_mounted_resources_verified": true,
  "macos_packaged_native_directory_dialog_cancel_then_chinese_clipboard_input_verified": true,
  "macos_packaged_backend_sha256": "59d7b0ebf7783e84057a9214a850e118ad49a48444bb2691baf3da08927f341c",
  "electron_final_companion_restart_source_in_each_platform_asar_verified": true,
  "frozen_companion_http_ws_stream_duplicate_and_reconnect_each_platform": true,
  "frozen_search_fetch_cache_protocol_each_platform": true,
  "frozen_two_images_plus_video_one_reply_each_platform": true,
  "frozen_protocol_tests_use_local_stubs_no_paid_api": true,
  "third_party_inventory_components_each_platform": 41,
  "third_party_license_files_macos": 65,
  "third_party_license_files_windows": 64,
  "retired_local_decision_weights_and_training_frameworks_excluded": true,
  "windows_setup_install_uninstall_cycle_run": false,
  "interactive_windows_ime_tested": false,
  "interactive_macos_ime_composition_tested": false,
  "real_account_login_tested": false,
  "paid_model_latency_or_total_savings_verified": false,
  "long_term_companion_quality_tested": false,
  "code_signed_or_notarized": false
}
```

未执行/边界：
- Windows x64 Setup/Portable and macOS arm64 DMG only; other desktop architectures were not built.
- Windows executables have no Authenticode signature; macOS has no Developer ID signature or notarization.
- NSIS full installation/uninstallation was not run; native unpacked and Portable execution are separately verified.
- Windows Chinese text was entered through the renderer; OS IME composition and real QQ/WeChat account login were not exercised. macOS verified Chinese clipboard input, not manual IME composition.
- Frozen model/search/media integration used local stubs without paid API calls; no fixed latency or bill-savings guarantee is inferred.
- Upgrade synthetic fixtures and one frozen v1.3.6 import are not a full lifecycle test of every historic installation; back up user data before covering an installation.
- macOS binary minimum deployment target is 11.0; this release was not run on macOS 11 hardware. Long-term companion quality remains an observation task.

## 2026-10-28：v3.0.2 本次发布记录

本次整合首页/设置重排、桌面伙伴关闭、历史日期和微信角色语音。源码、冻结后端、原生窗口、安装包与上传分别记录于[新版发布记录](../release-v3.0.2.json)，未执行项不沿用 v3.0.0 数字。v3.0.1 仅本地构建，不新增或修改其公开附件。

语音回归使用临时配置、模拟 Fish/微信服务与合成 PCM 夹具：可验证配置脱敏、显式引擎、WAV 校验、后台调度、冷却、退订、取消、上传加密和投递凭据复用。尚无真实 Fish Key 的付费合成、真实微信账号收件或手机端播放结果；文件附件不写成已验证的原生语音气泡。

冻结源码：`37cb904d477c536174b0df594df1b97019b7bd98`，源码归档 SHA256 为 `4a75515d5e969552533938dc9e82197fed64dcaabf1b402357be92a447a04daf`。本轮 macOS 源码回归 Python **1620项、跳过3项，20.592秒**通过；Node **21组**全部通过，主动联系前端专项为实际执行输入/变更处理器的12项行为检查。Windows 本机回归与安装包另按实际结果记录，这些源码数字不是实际客户端语音播放成绩。

### macOS arm64 本轮包验证

- 最终 `WxBot-3.0.2-macOS-arm64.dmg` 为 **158,892,753 bytes**，SHA256：`5ed82baedf4fca56e61a5237ee058bc67ca99ff0cf5ef9a8469dd802f068e63b`；`hdiutil verify` 返回 `VALID`。运行时保持上述冻结提交，外壳取 `70eba5fbfda2683151cd492190f397609b6f8fd9`，仅补新版根第三方声明。
- ASAR 9份源码、外层 web 29份和冻结后端 CArchive 中 web 29份逐字节匹配；实际第三方清单41组件、65份许可原文。
- 最终冻结应用实际 HTTP 六项资源、配置重启、未知文件保留与模拟 TTS 试听完成2次循环；HTTP/WS 伙伴单气泡、重复去重、重连和人格切换通过。
- 最终包原生伙伴的按钮、Esc、Cmd+W、重新打开及主程序继续运行通过。设置中文剪贴板输入和无Key试听诊断在相同运行时、声明补丁前的包验证；不写成手工IME组合输入测试。
- 所有 TTS/聊天/平台集成仍使用本地桩；未使用真实 Fish Key、未向真实微信联系人投递，也未验证手机端播放。安装包验证不等于这些账号级功能已经完成。

### Windows x64 本轮源码回归

原生 Python **1620项、跳过9项，253.881秒**通过。Node 中伙伴关闭测试首次遇到测试夹具硬编码 POSIX 缓存键与 Windows `path.join` 不一致；提交 `a657a08560db5344e182d0b971d2b60723977952` 只修正测试键的一行，不改变运行时。该专项在 macOS 重跑26项通过；Windows 全部21组Node已重跑通过，20组固定对话时间线独立补跑通过。不将此夹具修正记为运行时故障修复。

### Windows x64 最终包与当前边界

- 本次原生构建 Setup/Portable；ASAR 9份、外层 web 29份及冻结后端 CArchive 内 web 29份匹配，实际依赖41组件、64份许可。最终 Setup/Portable 各自 PE 内嵌7z再次解出核验：各9份ASAR源码、29份web及新版第三方声明逐字节一致；这是内嵌内容检查，不是NSIS安装/卸载交互。两端安装包的字节与SHA256列于[发布清单](../release-v3.0.2.json)，校验文件为本次独立生成，不覆盖旧版。
- 冻结后端3次重启验证鉴权、统计、Jieba、时区、引导、备份、记忆保留及正常shutdown；随后冻结后端2循环、Unpacked和Portable各1次实际HTTP验证六项资源逐字节一致、独立配置保存、未知文件保留与模拟TTS试听。付费外部API调用为0，测试资料与真实用户资料隔离。
- 本次单机启动样本：冻结后端约2.9—3.3秒、Unpacked 4.03秒、Portable首次冷展开40.41秒。Portable包含自解压，常用用户建议安装版；数字不是LLM响应延迟，也不泛化为所有硬件的承诺。
- Windows未执行交互界面、系统IME组合输入和NSIS完整安装/卸载；Unpacked/Portable测试结束按隔离进程树清理，不写成手动或优雅GUI退出验收。两端均未真实账号登录、Fish付费合成或微信手机端播放；包未配置代码签名/公证。

## 2026-10-28：v3.0.3 原生网关本轮验收

本轮运行时固定于 `2356227307c2f3eb426251b4f4bad8d7c3e91fc5`，与v3.0.2记录分开。

- macOS源码Python1696项（跳过3）、23.484秒通过；23组Node全部通过。NativeManager/API33项、进程43项、前端原生网关26项、下载准备18项均单独覆盖。
- Windows同源码完整Python1696项重跑通过（跳过30，255.339秒），Node23组通过。首轮只有旧的0.2秒硬时限夹具 `test_background_memory_does_not_block_next_reply` 在typing清理处超时；原断言不变，单项连跑三次及相同全套重跑均通过。首轮日志保留，不将重跑写成从未失败，也不由此承诺200ms性能。30项跳过包括平台专属与导出源码缺历史Git；时间线另行记录。
- Windows真实自有Job测试覆盖正常停止父子孙、启动器先退出、强杀宿主后清理；未收养的独立对照保持运行。进程模块Windows43项中21项为POSIX平台跳过。
- Windows原生Manager未登录三周期通过：完整705份发行文件及固定索引、鉴权、真实二维码仅获取未扫码、profile及未知文件保留、close保留恢复、手动stop关闭恢复、监听清理。QQ native产生唯一非执行beacon日志，已单独收集，不放宽其他额外代码文件与链接。
- macOS最终DMG验证VALID，9份ASAR、外层与CArchive各29份web字节一致；含原生管理Python模块但不含WindowsZIP或旧Docker部署模块。41个Python依赖、65份许可校验通过。实际冻结HTTP六资源、外部模式/不启动Docker、配置重启/未知文件保留和模拟语音试听两循环通过；冻结伙伴HTTP/WS单气泡、去重、重连和人格空间通过。原生窗口显示平台边界与连接控件；自动化下拉键盘选择不确定，不记为成功。
- 完整Windows安装包和冻结QQ API验收、精确摘要见[本轮清单](../release-v3.0.3.json)，不用源码成绩冒充真实账号收发。未登录真实QQ或向联系人发消息；Windows交互IME、NSIS安装/卸载、Fish付费与微信手机播放不记为已验证。

最终Windows冻结后端实际setup→Node/705全hash→WebUI鉴权与二维码→stop→再setup→shutdown退出并清Node→后端重启自动恢复→再次shutdown全部通过。两个安装器解出原始载荷，ASAR9/web29/冻结web29/固定官方NodeZIP及许可来源均一致。原有冻结后端两循环、Unpacked、Portable实际HTTP、配置/未知资料保留及四次本地模拟TTS通过；20时间线补跑通过。单机启动：Unpacked4.29秒、Portable冷展开46.76秒，QQ完整准备的三次观察16.42/9.63/11.42秒包含校验与轮询，不是LLM延迟。
