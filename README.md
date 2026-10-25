# 微信 AI 助手 · WxBot

[![最新发布](https://img.shields.io/github/v/release/Sirius-Peng/wx-bot)](https://github.com/Sirius-Peng/wx-bot/releases/latest)

**当前版本：v3.0.0（开发中） · 当前发布：v2.1.7 · 文档更新：2026-10-23。**

> **v3 尚未发布安装包。** 本页的 v3 功能说明对应当前源码，不代表下方 v2.1.7 下载包已包含这些功能。接话已改为规则保护与轻量云端判定，不再运行或随包提供本地判定模型；Windows/macOS 新版原生验收与覆盖升级验收尚待完成。见 [v3 实施与验收状态](docs/v3/README.md)。

微信私聊 + QQ 私聊 / 群聊 @ 的 AI 角色聊天机器人。自带 Web 管理界面、Electron 桌面版和弗洛洛人格预设，支持识图、连续消息合并、自然等待、长期记忆、联网搜索、主动联系和对话气泡统计。配置与记忆默认保存在安装目录以外。

[下载 v2.1.7](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.7) · [新手引导](docs/onboarding.md) · [完整文档](docs/README.md) · [变更记录](CHANGELOG.md)

## v3.0.0 源码重构

- **一个对话内核**：微信、QQ 与新增桌面文字伙伴共用收集、接话、记忆、生成和投递逻辑；桌面账号默认独立，不自动关联微信/QQ。保留首启引导、QQ 一键启动/快速登录、识图、联网总结、当前时间和旧资料导入。
- **界面收敛**：侧栏仅保留「首页」「接入设置」「人格与记忆」「设置」。人格、了解和简要词频/话题气泡放到同页；消息记录和引导从首页进入，移除完整拓扑界面。
- **更轻的记忆**：后台整理时同时分配重要性，低/普通/重要记忆分别在无新用户证据 **3/7/30 天**后淡化；手动固定不自动衰减。日常相关召回默认 **0—1 条**，明确追问最多三条、约400字。重要性不绕过渐进证据门槛，也不改写人格。
- **更快的接话链路**：完整表达默认 **2 秒**，碎片/附件说明 **4 秒**，自适应最高 **6 秒**；图片到达即预取、短时复用，减少重复下载。等待窗口内可提前进行可取消的轻量判定；完整人格与已发送会话保持稳定前缀，新轮动态资料追加在尾部。
- **桌面伙伴与快/深模式**：新增默认静态形象、Live2D 本地目录导入、桌面文字聊天和重连恢复；首版没有语音。日常默认 `fast`，可独立选择 `deep`；不替换已有模型、Key 或人格。
- **不再依赖本地判定模型**：明确诉求由规则保护，模糊私聊使用 `deepseek-flash` 非思考单请求、短 `R/D/C` 协议，判定总期限3秒；失败保守回应。不加载、预热或下载ONNX模型，历史研究工具与产物保留但不作为发行前提。见 [接话策略](docs/v3/local-decision.md)。
- **稳定会话与服务端缓存**：默认保持完整会话前缀，达到所配置窗口的40%才压缩，而不是8轮滑窗或5k即压缩；窗口可自动识别或手填。缓存只按服务真实usage计数，不承诺100%命中。见 [会话与KV缓存](docs/v3/context-session.md)。
- **减少无效调用、显示真实用量**：同一连续输入批次只开放一次云判定额度；搜索成功公开query短缓存60秒并合并相同在途请求，合格成稿不固定再复核。设置可手动查看近期真实usage、任务分类与token加权缓存比例；未知不补0，近期事件不是账单。不预报省费百分比，见 [v2.1.7费用核查与v3调整](docs/v3/cache-cost.md)。

现有数据目录、文件名、自定义路径、凭证和应用 ID `com.siriuspeng.wxbot` 保持不变。设计与当前待办见 [v3 总览](docs/v3/README.md)，原生验证结果单独记录，不用源码回归代替安装验收。

## 下载与平台

**最近已发布版本仍为 v2.1.7**，以下均为历史已发布附件；没有 v3 下载链接。v2.1.7 Windows x64安装版、便携版及macOS arm64 DMG均由对应原生平台构建。

| 平台 | 下载 | 发布大小 |
|---|---|---|
| Windows x64安装版 | [WxBot-2.1.7-Windows-x64-Setup.exe](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/WxBot-2.1.7-Windows-x64-Setup.exe) | 约 124.9 MiB |
| Windows x64便携版 | [WxBot-2.1.7-Windows-x64-Portable.exe](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/WxBot-2.1.7-Windows-x64-Portable.exe) | 约 124.7 MiB |
| macOS Apple Silicon | [WxBot-2.1.7-macOS-arm64.dmg](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/WxBot-2.1.7-macOS-arm64.dmg) | 约 151.0 MiB |
| SHA256 | [SHA256SUMS.txt](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/SHA256SUMS.txt) | 对应 v2.1.7 三个安装包 |

安装包自带运行时，无需Python / Node.js。v2.1.7没有macOS Intel、Windows ARM64/32位或Linux桌面包；Linux可用源码/Docker，其他架构构建入口见[桌面说明](desktop/README.md)。发布包未配置代码签名，首次打开流程见同页。历史Release与附件保持不变。

## 升级与旧数据

v3 源码仍使用下方原数据位置和应用 ID；正式覆盖安装流程须等 v3 云判定、升级夹具和原生包验收完成后再确认。测试源码前先备份资料，不以开发包替代已发布安装包。

macOS先从托盘退出旧应用，再将新App拖到「应用程序」替换。独立用户数据目录保持不变；建议先到「设置 → 本地数据与备份」（v2.1.7 为「模型与行为」）创建备份。Windows先退出旧应用，再使用v2.1.7安装版覆盖安装；便携版退出后替换旧程序。两种方式均继续使用独立用户数据目录。

v2.1.4起支持「设置 → 导入旧版数据」（v2.1.7 为「模型与行为」）：选择包含 `config.json` 的旧目录，预览并确认整套替换，准备导入后重启后台。导入前自动备份当前资料，旧目录不修改；保留配置、人格、记忆、上下文、聊天记录及普通文件，QQ Docker卷另行备份。详见[导入与恢复](docs/data-import.md)。

## 零基础开始

以下平台登录与 Key 流程沿用 v2.1.7；当前 v3 源码将相应卡片归入四个主页面，下载包的菜单名称仍以其版本为准。

1. **安装并打开**应用，首次配置进入「上手引导」，选择微信、QQ 或两者。引导进度保存在本地，暂时跳过后可继续。
2. **配置模型**：默认地址 `https://api.deepseek.com`，API 模型名 `deepseek-flash`。在 [DeepSeek 开放平台](https://platform.deepseek.com/)登录，到 [API keys](https://platform.deepseek.com/api_keys) 创建 Key，检查余额后粘贴到引导中并点击「保存并测试模型」。测试会产生少量 API 用量；聊天网页版登录不等于已经配置 API。费用以[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)为准，不作固定预算或免费承诺。
3. **连接平台**：微信 iLink 使用手机微信扫码，无需独立网关或手填平台 Token。QQ 点首页「一键启动 QQ 网关」，按提示准备 Docker/NapCat；已保存账号可快速登录，首次或失效时扫码。二维码实际准备好且图片解码后才弹窗。
4. **完成并启动**机器人，发送「你好」或图片验证。已有登录会话尽量复用，是否需要重新确认由平台决定。

首次安装 Docker/启动服务可能需要系统确认；应用显示当前进度。网关的一键启动不会替用户完成手机确认。详细 URL、Token、镜像、快速登录账号及日志仍在「接入设置」；模型、识图、搜索、人格、行为与记忆各有自己的保存按钮。

1017联网更新：默认使用官方直接 Search / Fetch，不再为检索额外调用辅助搜索模型；保留兼容选项与原Key/模型配置。工具按次收费，不承诺免费或省费比例，见[联网升级说明](docs/v3/search-1017.md)。

## 当前功能

| 功能 | 当前 v3 源码行为（未发布） |
|---|---|
| 微信 | 本项目采用腾讯 iLink 私聊通道，长轮询收件；不提供微信群聊或 GeWe 通道 |
| QQ | NapCat / OneBot 11，私聊回复、群聊仅 @ 机器人时回复；反向 WebSocket 或 HTTP 上报 |
| 完整回复 | 等模型完成后发送一条完整消息；普通聊天合并多余换行，代码、列表等保留格式 |
| 连续输入 | 同一发言者有序合并文字和图片；新消息重置静默窗口，明确等待/结束信号影响作答时机 |
| 输入提示 | 真正开始处理后使用平台支持的原生正在输入状态；辅助失败不阻塞正文，不发送占位气泡 |
| 图片识别 | 下载/解密并归一化真实图片后传给视觉模型；独立视觉配置为空时复用主模型，主模型须支持图像输入 |
| 人格 | 默认弗洛洛，支持关系模式、场景少样本和自定义人格；提示词拟合，不是模型权重微调 |
| 长期记忆 | 经历→候选印象→稳定认知；跨独立对话、相隔24小时才巩固；来源追溯、衰减休眠、手动固定和持久遗忘 |
| 跨端连续性 | 微信/QQ/桌面共用长期库；同一个人的账号需明确关联，不凭昵称猜测身份；单端用户无需关联 |
| 上下文 | 完整人格与已发送会话前缀稳定追加；默认达到模型窗口40%后才压缩，窗口自动/手填、压缩比例可配置；当轮资料有界，当前来话与媒体不静默截断 |
| 自然接话 | 明确问题/请求/情绪诉求保障回应，确认或收尾可静默；普通分享1～3分钟重判一次，和随机主动联系独立 |
| 主动联系 | 仅微信或仅QQ均可；可选同人关联共享计划、冷却/免打扰及触达预约，未回复不连续催促 |
| 联网与时间 | 默认官方直接 Search / Fetch，正文/摘要依据后一次整合；合计最多两次工具调用、按次额外计费；手动可选旧版 native，每轮参考当前时间，默认 `Asia/Shanghai` |
| 桌面伙伴 | 首页开启独立桌面会话；默认静态形象或本地 Live2D 模型，文字聊天、草稿/完成状态、重连与去重；首版没有语音 |
| 对话统计 | 「人格与记忆」内的简要高频词/话题气泡与排行；本地 Jieba 分词及关键词规则，不调用 LLM |
| 数据与备份 | 独立本地目录、非覆盖迁移、校验过的 ZIP 备份；普通更新/卸载不主动删除用户数据 |

普通持续输入默认静默 **2秒**，碎片、未完成句和附件说明 **4秒**，结合最近20个有效发送间隔上调，最多 **6秒**。原 **10秒**仅内部评估，不强制发出旧稿；50条只是预处理分段，不切断发言。明确说「等我说完」默认等待 **120秒**后重判，「说完了」约0.3秒接答；窗口设0可关闭合并。生成、识图、搜索和令牌等待中收到新输入，会使旧的未提交稿失效；提交后沿用原投递凭据。

默认「自然回复」和「稍后自然接话」，可在「设置 → 回复」切回每轮必回。静默消息仍进入用户上下文，等待和静默不显示假打字。人格身份、口吻与价值观不由记忆改写；日常相关记忆默认0—1条，明确追问最多3条、约400字。至少三轮有效表达、空闲两分钟后后台整理；稳定认知需要两次独立对话（相隔至少24小时）。记忆按重要性而非仅按形成阶段决定寿命，低/普通/重要分别在3/7/30天无新用户证据后休眠，半衰期分别1/2/7天；助手复述、检索或模型重评分不续期；只有管理页手动添加/固定立即生效。详见[渐进记忆与人格优先](docs/memory-design.md)和[自然接话](docs/image-turn-taking.md)。

微信草稿、入站队列和 QQ 等待队列可持久恢复。微信发送检查 HTTP 与业务状态，并复用正文、`client_id` 和 `context_token` 重试；接口接受不等于客户端已读，进程在外部成功和本地确认之间中断仍可能重复。详情见[投递与输入状态](docs/ilink-delivery-check.md)。

## 管理界面

| 侧栏页面 | 当前源码内容 |
|---|---|
| 首页 | 运行/投递状态、QQ 一键启动、桌面伙伴开关/导入/打开、消息记录和上手引导入口 |
| 接入设置 | 微信登录、QQ 扫码/快速登录、网关与推送状态、日志 |
| 人格与记忆 | 人格设定独立保存；对用户的了解、形成中/已熟悉/已淡化/已固定、来源、固定/遗忘、可选身份关联、简要词频/话题气泡 |
| 设置 | 主/备用/判定/视觉模型、联网搜索与时区、fast/deep、回复行为、主动联系、记忆、备份/导入、桌面管理；卡片独立保存 |

完整拓扑界面已从 v3 源码移除；[v2.1.7 拓扑说明](docs/memory-topology.md)仅供旧版用户。旧 `#graph` / `#statistics` 入口转到记忆页，不删除已有用户资料。

## 模型与数据流

- 主模型处理聊天。明显问题、请求、纠正和情绪诉求由保护规则直接保障回应；只有模糊私聊使用轻量云端判定。默认 `deepseek-flash`、非思考、单请求、短 `R/D/C` 协议，整个判定阶段最多3秒；超时、截断或解析异常保守回应，不发送诊断。独立判定配置继续保留，未配置时按现有规则复用主模型/Key，不覆盖用户配置。
- 日常 `fast` 对已识别支持的官方 DeepSeek 接口关闭思考；`deep` 是单独开关。不向未知兼容接口强塞供应商专用参数，不擅自更换用户模型或 Key。
- 独立视觉配置留空时使用主模型。官方 `deepseek-flash` 的图像内容块依据见[视觉文档](https://api-docs.deepseek.com/guides/vision/)；其他兼容接口的视觉/工具能力需自行配置和验证。
- 判定只读本轮及必要的少量会话状态，不读取人格全文或知识库。等待窗口内先防抖约0.8秒再与图片预取/本地检索并行；同一连续输入批次只开放一次云判定额度，同一有效结果仅交接一次。新来话取消旧版本且不复用旧静默结论；额度已用时最新全文仍走保护规则，模糊则保守回应，不逐碎片重发。运行与安装包不使用ONNX/tokenizers或本地判定权重。
- 搜索 Key 留空时，仅官方 DeepSeek 主接口可复用主 Key；使用其他供应商主模型时需要单独配置 DeepSeek 搜索 Key。每轮最多两次辅助搜索；后台抽取、摘要与引导连通测试不搜索。
- 聊天文本、图像和相关记忆会按功能发给所配置的模型服务；“本地保存”不表示模型推理全部离线。搜索只提交必要公开 query，不把整段聊天、记忆或图片用作搜索查询。
- 默认参考时间来自本机时钟，不推断用户所在时区。统计读取本地持久消息日志，话题为多标签关键词类别，不是 LLM 语义分类；一次最多扫描最近 32 MiB，达到限额时会提示。

### 会话前缀与KV缓存

默认不再每8轮滑动历史，也不以5,000 token为固定摘要阈值。设置可选择上下文窗口自动识别或手填，并调整压缩触发比例（默认40%）。截至2026-10-23，官方DeepSeek的 `deepseek-flash` / `deepseek-v4-pro` 均标注1M上下文；其他未知模型自动采用32K保守值，仍可按实际供应商窗口手动修改。[官方模型说明](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

每轮追加新的时间、检索与媒体状态，不回写此前已经发送的前缀；压缩、明确遗忘、人格或模型/供应商变更会使旧本地会话失效，不跨用户复用。DeepSeek服务端缓存是尽力而为，HTTP连接复用不等于KV命中；只记录 `usage` 返回的真实hit/miss token，缺失为未知。完整边界见 [稳定会话与KV缓存](docs/v3/context-session.md)。

在「设置 → 主模型 → 用量与缓存诊断」手动刷新当前运行近期最多500条事件；按任务查看输入、输出和实际缓存用量。缓存率按已知输入token加权，覆盖不全时总体未知；思考为输出子集，不重复相加。这不是账户历史账单，不按模型名估价。用户反馈v2.1.7约17M token/30元缺少用量拆分，不能反推真实缓存率；旧版问题与当前源码调整见[费用核查](docs/v3/cache-cost.md)，不会自动改变旧包。

## 本地数据、迁移与备份

| 平台 | 默认活动数据目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\WxBot\user-data` |
| macOS | `~/Library/Application Support/WxBot/user-data` |
| Linux | `$XDG_DATA_HOME/wxbot/user-data`；未配置时 `~/.local/share/wxbot/user-data` |

目录包含配置、人格设置、记忆、上下文、消息记录、引导进度、主动联系计划与近期互动、微信凭证与待处理状态；v3视觉会话另有本地私有媒体sidecar，主上下文JSON保留引用，整套迁移需一并保留；桌面后端日志在其 `logs/backend.log`。QQ 登录资料另外由 NapCat 持久卷管理。显式 `WXBOT_DATA_DIR` 优先；`WXBOT_USER_DATA_DIR` 对应 `<指定目录>/data`。

首次使用默认新目录时自动复制可识别旧数据，保留来源和迁移备份；已有目标文件不覆盖。卸载/更新不主动删除默认独立目录；若自定义到安装目录内则不适用同样的保留前提。手动删除、磁盘故障或误覆盖仍需备份恢复。POSIX 敏感文件按私有权限创建，Windows 依用户目录 ACL。在「设置 → 本地数据与备份」（v2.1.7 为「模型与行为」）查看实际目录并创建本地 ZIP；备份包含密钥和聊天资料，不自动上传。

恢复前先退出应用并保留当前文件备份；详细步骤、外部微信目录及 Docker 持久卷见[数据持久化](docs/user-data-persistence.md)。

## 源码运行

源码使用 **Python 3.10+**；桌面开发/构建另需 Node.js。安装版用户无需这些工具。

```sh
python -m venv .venv
# macOS/Linux
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m src.server
```

```powershell
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m src.server
```

也可双击 `start-mac.command`、`start-windows.bat`，Linux 执行 `./start.sh`。脚本安装依赖的时长随环境/下载变化，不保证固定几秒。默认网页地址 `http://127.0.0.1:8000`，源码端口被占用时会顺延；以启动日志为准。桌面应用优先使用 `17800`，占用时使用空闲端口，由应用维护网关回调地址。

### Docker

```sh
docker compose up -d
```

默认只发布宿主机 `127.0.0.1:8000`。活动资料绑定宿主机 `${HOME}/.local/share/wxbot/user-data`，可用 `WXBOT_HOST_DATA_DIR` 改为绝对路径；旧 `./data` 只读挂载供迁移。Windows 可先设置：

```powershell
$env:WXBOT_HOST_DATA_DIR = "$env:LOCALAPPDATA\WxBot\user-data"
docker compose up -d
```

微信无需单独网关容器。QQ 的自动容器部署需 Docker，并按 Compose 注释提供 Docker socket；也可使用已配置的外部 OneBot 网关。本程序无需额外数据库，但 LLM、微信及 QQ 网关仍是外部服务。

### 环境变量

| 变量 | 默认/用途 |
|---|---|
| `WXBOT_HOST` / `WXBOT_PORT` | 源码 `127.0.0.1` / `8000`；桌面和 Compose 自行设置 |
| `WXBOT_PREFERRED_PORT` | 桌面首选 `17800`，不可用时回退空闲端口 |
| `WXBOT_DATA_DIR` | 活动数据绝对/自定义目录，优先于平台默认路径 |
| `WXBOT_USER_DATA_DIR` | 显式 profile 隔离；活动数据为其 `data` 子目录 |
| `WXBOT_HOST_DATA_DIR` | Compose 宿主机数据绑定位置，不是 Python 通用配置 |
| `WXBOT_ADMIN_TOKEN` | 管理认证 Token；未指定时数据目录生成 `admin-token` |
| `WXBOT_NO_BROWSER=1` | 源码不自动打开浏览器；桌面由外壳托管 |
| `WXBOT_MIGRATE_LEGACY` / `WXBOT_LEGACY_DATA_DIRS` | 控制旧数据复制迁移；后者为 JSON 路径数组 |
| `WXBOT_SKIP_NAPCAT_SYNC=1` | 离线测试跳过网关配置同步，不是正常使用默认值 |

本机打开主页可建立 HttpOnly 管理会话；远程/容器使用管理登录页或 Bearer Token。OneBot `/ws/onebot`、`/onebot/event` 使用独立 OneBot Token。Windows 桌面 Docker 回调使用单独的 callback-only 动态端口，管理页面仍只监听本机；仅配置 OneBot HTTP 地址不代表消息推送已连接。

## 构建与验证

桌面原生构建、资源布局、托盘和退出说明见 [desktop/README.md](desktop/README.md)。Windows 后端使用 x64 **onedir**，macOS 使用 **onefile**；不能拿 macOS sidecar 拼进 Windows 包。

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_desktop_port.cjs
node tests/test_windows_desktop.cjs
node tests/test_windows_lifecycle.cjs
node tests/test_storage_paths.cjs
node tests/test_onboarding_frontend.cjs
node tests/test_statistics_frontend.cjs
node tests/test_data_import_frontend.cjs
node tests/test_input_focus.cjs
node tests/test_proactive_frontend.cjs
node tests/test_natural_frontend.cjs
node tests/test_core_ui_v3.cjs
node tests/test_cloud_package.cjs
node desktop/scripts/verify-runtime-resources.cjs --source-only
python -m unittest tests.test_core_v3 tests.test_memory_fast_v3
python -m tools.smoke_companion_v3
python tools/smoke_ilink.py
python -m src.verify_qq
python tools/qr_check.py
python tools/check_docs.py
```

以上 `python` 应使用已安装项目依赖的虚拟环境。`check_docs.py` 为文档一致性检查，不调用模型或平台；详细验证范围见[测试矩阵](docs/validation.md)。

运行与打包统一使用根目录 `requirements.txt`，无需额外数据库依赖清单。Docker构建上下文仅包含运行源码和依赖清单，不传入本地安装包、虚拟环境、Git历史或用户数据。人格合成语料由内置示例按需导出：`python tools/export_persona.py`，默认输出到已忽略的 `personas/phrolova/generated/`；详见[人格评估与导出](personas/phrolova/EVALUATION.md)。

**历史 v2.1.7 发布验收**：macOS和Windows原生源码各755项Python回归（各跳过1项）、11组Node回归通过，含55项拓扑前端检查和20组固定对话时间线。两端冻结后端的只读图谱、自然设置、记忆v3、固定/遗忘与重启，以及主动联系、旧资料导入和包内源码/资源一致性另行核验；Windows便携版原生进程/API启动、macOS DMG只读挂载验证通过。完整边界与精确摘要见[v2.1.7 发布清单](docs/release-v2.1.7.json)和[测试矩阵](docs/validation.md)。

v3 开发验收独立记录于 [验收状态](docs/v3/validation.md)：源码回归和模拟桌面链路不等于三平台原生验收。历史本地模型实验已退役，保留研究记录；当前云判定、稳定会话与原生包须另行验收，未产出 v3 正式安装包。

## 项目结构

```text
src/server.py, runtime.py       Web/API、运行时、账号与投递诊断
src/adapters/                  iLink、OneBot 11 与桌面会话
src/core/conversation.py       有序图文与连续输入批次
src/core/turn_taking.py         本地轮流说话/等待规则
src/core/turn_coordinator.py    持久会话、决策与稍后接话
src/core/reply_decision.py      保护规则 / 轻量云判定 / 失败保守回应
src/core/local_decision*.py     已退役研究代码，不进入应用运行或安装包
src/core/media.py               图片下载、解密与视觉输入
src/core/context.py, memory.py  人格/上下文/长期事实与身份关联
src/core/memory_learning.py     后台渐进记忆整理
src/core/companion_models.py    Live2D 本地目录导入与资源校验
src/core/web_search.py          DeepSeek 辅助搜索
src/core/reality.py             每轮现实时间
src/core/analytics.py           本地词频/话题统计
src/core/storage.py, fileio.py  独立目录、迁移/备份、Windows快照
src/web/                       原生 HTML/CSS/JS 管理界面
desktop/                       Electron 管理/伙伴窗口与原生打包
tools/decision_model/          云判定对比工具及已退役本地训练研究资料
personas/phrolova/              人格卡、合成训练格式与评估资料
tests/, tools/                 离线回归、平台/打包和文档核验
docs/, CHANGELOG.md             使用、设计、验证与版本历史
```

此仓库提供本地文件形态的长期记忆与轻量词法/结构化召回，未接入向量数据库或托管记忆服务。下载包分别固定对应其发布标签：最近已发布macOS和Windows包均为`v2.1.7`，尚无`v3.0.0`包；主分支文档更新不会改动旧包或移动标签。

第三方渲染资源及模型来源分别按其自身条款处理，项目元数据中的 MIT 不替代第三方条款；见 [v3 第三方清单与发布核对草案](docs/v3/third-party.md)。

## 作者与贡献者

<a href="https://github.com/Sirius-Peng"><img src="https://avatars.githubusercontent.com/u/54427718?v=4" width="64" height="64" alt="Sirius-Peng" /></a>

**[Sirius-Peng（Yuhan Peng）](https://github.com/Sirius-Peng)** — 项目作者与维护者。

[贡献者说明](CONTRIBUTORS.md) · [参与贡献](CONTRIBUTING.md) · [GitHub贡献者统计](https://github.com/Sirius-Peng/wx-bot/graphs/contributors)
