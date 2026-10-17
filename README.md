# 微信 AI 助手 · WxBot

[![最新发布](https://img.shields.io/github/v/release/Sirius-Peng/wx-bot)](https://github.com/Sirius-Peng/wx-bot/releases/latest)

**当前版本：v2.1.7 · 当前发布：v2.1.7 · 更新：2026-10-17。**

微信私聊 + QQ 私聊 / 群聊 @ 的 AI 角色聊天机器人。自带 Web 管理界面、Electron 桌面版和弗洛洛人格预设，支持识图、连续消息合并、自然等待、长期记忆、联网搜索、主动联系和对话气泡统计。配置与记忆默认保存在安装目录以外。

[下载 v2.1.7](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.7) · [新手引导](docs/onboarding.md) · [完整文档](docs/README.md) · [变更记录](CHANGELOG.md)

## v2.1.7 本次更新

- **人格优先、渐进记忆 v3**：从交流经历形成候选印象，跨独立对话的用户证据才巩固；助手复述和检索不续期，支持衰减、休眠、固定及持久遗忘屏蔽。旧记忆迁移前备份，保留现有人格与独立数据目录。
- **自然接话与连续输入**：私聊先听完整段表达，再判断回应、继续等待或仅留上下文；普通分享可稍后重判。生成、识图、搜索和限流等待中有新输入时废弃未提交旧稿，QQ私聊改为12条/分钟、突发2。
- **只读记忆拓扑**：查看对话、原话依据、记忆与关键词的真实关联；参考 Obsidian 的图优先交互，支持局部1～3层、悬停聚焦、拖动缩放、筛选、全屏和按需详情，不强化记忆、不新增模型费用。
- 默认启用自然回复和稍后接话，同时保留「每轮必回」；QQ群聊仍只在被@时回应。详见[渐进记忆](docs/memory-design.md)、[自然接话](docs/image-turn-taking.md)、[拓扑图](docs/memory-topology.md)。

## 下载与平台

本次发布 **v2.1.7 Windows x64安装版、便携版及macOS arm64 DMG**，均由对应原生平台构建。

| 平台 | 下载 | 发布大小 |
|---|---|---|
| Windows x64安装版 | [WxBot-2.1.7-Windows-x64-Setup.exe](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/WxBot-2.1.7-Windows-x64-Setup.exe) | 约 124.9 MiB |
| Windows x64便携版 | [WxBot-2.1.7-Windows-x64-Portable.exe](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/WxBot-2.1.7-Windows-x64-Portable.exe) | 约 124.7 MiB |
| macOS Apple Silicon | [WxBot-2.1.7-macOS-arm64.dmg](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/WxBot-2.1.7-macOS-arm64.dmg) | 约 151.0 MiB |
| SHA256 | [SHA256SUMS.txt](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.7/SHA256SUMS.txt) | 对应本次三个安装包 |

安装包自带运行时，无需Python / Node.js。本次没有macOS Intel、Windows ARM64/32位或Linux桌面包；Linux可用源码/Docker，其他架构构建入口见[桌面说明](desktop/README.md)。发布包未配置代码签名，首次打开流程见同页。历史Release与附件保持不变。

## 升级与旧数据

macOS先从托盘退出旧应用，再将新App拖到「应用程序」替换。独立用户数据目录保持不变；建议先到「模型与行为 → 本地数据与备份」创建备份。Windows先退出旧应用，再使用v2.1.7安装版覆盖安装；便携版退出后替换旧程序。两种方式均继续使用独立用户数据目录。

v2.1.4起支持「模型与行为 → 导入旧版数据」：选择包含 `config.json` 的旧目录，预览并确认整套替换，准备导入后重启后台。导入前自动备份当前资料，旧目录不修改；保留配置、人格、记忆、上下文、聊天记录及普通文件，QQ Docker卷另行备份。详见[导入与恢复](docs/data-import.md)。

## 零基础开始

1. **安装并打开**应用，首次配置进入「上手引导」，选择微信、QQ 或两者。引导进度保存在本地，暂时跳过后可继续。
2. **配置模型**：默认地址 `https://api.deepseek.com`，API 模型名 `deepseek-flash`。在 [DeepSeek 开放平台](https://platform.deepseek.com/)登录，到 [API keys](https://platform.deepseek.com/api_keys) 创建 Key，检查余额后粘贴到引导中并点击「保存并测试模型」。测试会产生少量 API 用量；聊天网页版登录不等于已经配置 API。费用以[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)为准，不作固定预算或免费承诺。
3. **连接平台**：微信 iLink 使用手机微信扫码，无需独立网关或手填平台 Token。QQ 点首页「一键启动 QQ 网关」，按提示准备 Docker/NapCat；已保存账号可快速登录，首次或失效时扫码。二维码实际准备好且图片解码后才弹窗。
4. **完成并启动**机器人，发送「你好」或图片验证。已有登录会话尽量复用，是否需要重新确认由平台决定。

首次安装 Docker/启动服务可能需要系统确认；应用显示当前进度。网关的一键启动不会替用户完成手机确认。详细 URL、Token、镜像、快速登录账号及日志仍在「接入设置」；模型、识图、搜索、人格、行为与记忆各有自己的保存按钮。

## 当前功能

| 功能 | v2.1.7 实际行为 |
|---|---|
| 微信 | 本项目采用腾讯 iLink 私聊通道，长轮询收件；不提供微信群聊或 GeWe 通道 |
| QQ | NapCat / OneBot 11，私聊回复、群聊仅 @ 机器人时回复；反向 WebSocket 或 HTTP 上报 |
| 完整回复 | 等模型完成后发送一条完整消息；普通聊天合并多余换行，代码、列表等保留格式 |
| 连续输入 | 同一发言者有序合并文字和图片；新消息重置静默窗口，明确等待/结束信号影响作答时机 |
| 输入提示 | 真正开始处理后使用平台支持的原生正在输入状态；辅助失败不阻塞正文，不发送占位气泡 |
| 图片识别 | 下载/解密并归一化真实图片后传给视觉模型；独立视觉配置为空时复用主模型，主模型须支持图像输入 |
| 人格 | 默认弗洛洛，支持关系模式、场景少样本和自定义人格；提示词拟合，不是模型权重微调 |
| 长期记忆 | 经历→候选印象→稳定认知；跨独立对话、相隔24小时才巩固；来源追溯、衰减休眠、手动固定和持久遗忘 |
| 跨端连续性 | 微信/QQ 共用长期库；同一个人的账号需在「人格知识库」明确关联，不凭昵称猜测身份 |
| 上下文 | 私聊近期问答与后台摘要；关联同人的跨端近期互动可参考，群聊及未关联用户仍隔离 |
| 自然接话 | 明确问题/请求/情绪诉求保障回应，确认或收尾可静默；普通分享1～3分钟重判一次，和随机主动联系独立 |
| 主动联系 | 仅微信或仅QQ均可；可选同人关联共享计划、冷却/免打扰及触达预约，未回复不连续催促 |
| 联网与时间 | 默认按需 DeepSeek 辅助搜索，整合正文事实并复核，避免标题与链接堆砌；每轮注入当前时间，默认 `Asia/Shanghai` |
| 记忆拓扑 | [对话、来源、记忆与关键词的关联图](docs/memory-topology.md)，支持对象/渠道/阶段筛选与来源追溯；只读、不强化记忆 |
| 对话统计 | 高频词/话题气泡图、排行和筛选；本地 Jieba 精确分词及关键词规则，不调用 LLM |
| 数据与备份 | 独立本地目录、非覆盖迁移、校验过的 ZIP 备份；普通更新/卸载不主动删除用户数据 |

普通持续输入默认静默 **4秒**，碎片、未完成句和附件说明 **6秒**，结合最近20个有效发送间隔上调，最多 **8秒**。原 **10秒**仅内部评估，不强制发出旧稿；50条只是预处理分段，不切断发言。明确说「等我说完」默认等待 **120秒**后重判，「说完了」约0.3秒接答；窗口设0可关闭合并。生成、识图、搜索和令牌等待中收到新输入，会使旧的未提交稿失效；提交后沿用原投递凭据。

默认「自然回复」和「稍后自然接话」，可在「模型与行为 → 回复」切回每轮必回。静默消息仍进入用户上下文，等待和静默不显示假打字。人格身份、口吻与价值观不由记忆改写；记忆默认最多3条、约400字。至少三轮有效表达、空闲两分钟后后台整理；稳定认知需要两次独立对话（相隔至少24小时）。候选14天、稳定180天无新用户证据后休眠，助手复述或检索不续期；只有管理页手动添加/固定立即生效。详见[渐进记忆与人格优先](docs/memory-design.md)和[自然接话](docs/image-turn-taking.md)。

微信草稿、入站队列和 QQ 等待队列可持久恢复。微信发送检查 HTTP 与业务状态，并复用正文、`client_id` 和 `context_token` 重试；接口接受不等于客户端已读，进程在外部成功和本地确认之间中断仍可能重复。详情见[投递与输入状态](docs/ilink-delivery-check.md)。

## 管理界面

| 页面 | 内容 |
|---|---|
| 概览 | 运行/投递状态、最近消息、首页单按钮启动 QQ 网关 |
| 接入设置 | 通道、微信登录、QQ 扫码/快速登录、网关与推送状态、日志 |
| 人格知识库 | 对用户的了解、形成中/已熟悉/已淡化/已固定、证据来源、固定/遗忘、后台整理及可选跨端关联 |
| 记忆拓扑 | [对象、近期对话/来源摘录、记忆与关键词](docs/memory-topology.md)，全局/局部探索、悬停聚焦、来源详情、筛选、拖动缩放和全屏 |
| 消息记录 | 用户/机器人记录、触发结果、发送者 ID |
| 对话统计 | 日期、平台、消息方向、私聊/群聊、前 N 项和最低频次；词频/话题气泡与排行 |
| 模型与行为 | 主/备用/判定/视觉模型、联网搜索与时区、人格、回复行为、主动联系与联系人、记忆、本地备份、桌面管理 |
| 上手引导 | 平台选择、Key 教程、主模型测试、账号登录、核验并启动；可重新打开 |

## 模型与数据流

- 主模型处理聊天。独立判定/抽取模型 Key 留空时复用主模型；设置独立 Key 后使用独立模型，不保证该服务免费。
- 独立视觉配置留空时使用主模型。官方 `deepseek-flash` 的图像内容块依据见[视觉文档](https://api-docs.deepseek.com/guides/vision/)；其他兼容接口的视觉/工具能力需自行配置和验证。
- 搜索 Key 留空时，仅官方 DeepSeek 主接口可复用主 Key；使用其他供应商主模型时需要单独配置 DeepSeek 搜索 Key。每轮最多两次辅助搜索；后台抽取、摘要与引导连通测试不搜索。
- 聊天文本、图像和相关记忆会按功能发给所配置的模型服务；“本地保存”不表示模型推理全部离线。搜索只提交必要公开 query，不把整段聊天、记忆或图片用作搜索查询。
- 默认参考时间来自本机时钟，不推断用户所在时区。统计读取本地持久消息日志，话题为多标签关键词类别，不是 LLM 语义分类；一次最多扫描最近 32 MiB，达到限额时会提示。

## 本地数据、迁移与备份

| 平台 | 默认活动数据目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\WxBot\user-data` |
| macOS | `~/Library/Application Support/WxBot/user-data` |
| Linux | `$XDG_DATA_HOME/wxbot/user-data`；未配置时 `~/.local/share/wxbot/user-data` |

目录包含配置、人格设置、记忆、上下文、消息记录、引导进度、主动联系计划与近期互动、微信凭证与待处理状态；桌面后端日志在其 `logs/backend.log`。QQ 登录资料另外由 NapCat 持久卷管理。显式 `WXBOT_DATA_DIR` 优先；`WXBOT_USER_DATA_DIR` 对应 `<指定目录>/data`。

首次使用默认新目录时自动复制可识别旧数据，保留来源和迁移备份；已有目标文件不覆盖。卸载/更新不主动删除默认独立目录；若自定义到安装目录内则不适用同样的保留前提。手动删除、磁盘故障或误覆盖仍需备份恢复。POSIX 敏感文件按私有权限创建，Windows 依用户目录 ACL。在「模型与行为 → 本地数据与备份」查看实际目录并创建本地 ZIP；备份包含密钥和聊天资料，不自动上传。

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
node tests/test_memory_graph_frontend.cjs
python tools/smoke_ilink.py
python -m src.verify_qq
python tools/qr_check.py
python tools/check_docs.py
```

以上 `python` 应使用已安装项目依赖的虚拟环境。`check_docs.py` 为文档一致性检查，不调用模型或平台；详细验证范围见[测试矩阵](docs/validation.md)。

运行与打包统一使用根目录 `requirements.txt`，无需额外数据库依赖清单。Docker构建上下文仅包含运行源码和依赖清单，不传入本地安装包、虚拟环境、Git历史或用户数据。人格合成语料由内置示例按需导出：`python tools/export_persona.py`，默认输出到已忽略的 `personas/phrolova/generated/`；详见[人格评估与导出](personas/phrolova/EVALUATION.md)。

**v2.1.7发布验收**：macOS和Windows原生源码各755项Python回归（各跳过1项）、11组Node回归通过，含55项拓扑前端检查和20组固定对话时间线。两端冻结后端的只读图谱、自然设置、记忆v3、固定/遗忘与重启，以及主动联系、旧资料导入和包内源码/资源一致性另行核验；Windows便携版原生进程/API启动、macOS DMG只读挂载验证通过。完整边界与精确摘要见[当前发布清单](docs/release-v2.1.7.json)和[测试矩阵](docs/validation.md)。

## 项目结构

```text
src/server.py, runtime.py       Web/API、运行时、账号与投递诊断
src/adapters/                  iLink 与 OneBot 11
src/core/conversation.py       有序图文与连续输入批次
src/core/turn_taking.py         本地轮流说话/等待规则
src/core/turn_coordinator.py    持久会话、决策与稍后接话
src/core/reply_decision.py      私聊自然回应判定
src/core/media.py               图片下载、解密与视觉输入
src/core/context.py, memory.py  人格/上下文/长期事实与身份关联
src/core/memory_learning.py     后台渐进记忆整理
src/core/memory_graph.py        只读记忆关联快照
src/core/web_search.py          DeepSeek 辅助搜索
src/core/reality.py             每轮现实时间
src/core/analytics.py           本地词频/话题统计
src/core/storage.py, fileio.py  独立目录、迁移/备份、Windows快照
src/web/                       原生 HTML/CSS/JS 管理界面
desktop/                       Electron 外壳与原生打包脚本
personas/phrolova/              人格卡、合成训练格式与评估资料
tests/, tools/                 离线回归、平台/打包和文档核验
docs/, CHANGELOG.md             使用、设计、验证与版本历史
```

此仓库提供本地文件形态的长期记忆与轻量词法/结构化召回，未接入向量数据库或托管记忆服务。下载包分别固定对应其发布标签：本次macOS和Windows包均为`v2.1.7`；主分支文档更新不会改动旧包或移动标签。

## 作者与贡献者

<a href="https://github.com/Sirius-Peng"><img src="https://avatars.githubusercontent.com/u/54427718?v=4" width="64" height="64" alt="Sirius-Peng" /></a>

**[Sirius-Peng（Yuhan Peng）](https://github.com/Sirius-Peng)** — 项目作者与维护者。

[贡献者说明](CONTRIBUTORS.md) · [参与贡献](CONTRIBUTING.md) · [GitHub贡献者统计](https://github.com/Sirius-Peng/wx-bot/graphs/contributors)
