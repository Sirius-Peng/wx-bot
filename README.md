# 微信 AI 助手 · WxBot

[![最新发布](https://img.shields.io/github/v/release/Sirius-Peng/wx-bot)](https://github.com/Sirius-Peng/wx-bot/releases/latest)

**当前版本：v2.1.5 · 当前发布：v2.1.5 · 更新：2026-10-15。**

微信私聊 + QQ 私聊 / 群聊 @ 的 AI 角色聊天机器人。自带 Web 管理界面、Electron 桌面版和弗洛洛人格预设，支持识图、连续消息合并、自然等待、长期记忆、联网搜索、主动联系和对话气泡统计。配置与记忆默认保存在安装目录以外。

[下载 v2.1.5](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.5) · [新手引导](docs/onboarding.md) · [完整文档](docs/README.md) · [变更记录](CHANGELOG.md)

## v2.1.5 本次更新

- **联网先总结再回答**：保留搜索服务的正文总结与多段引用，整合事实、合并重复报道并复核；默认只附一条核心来源，不再甩出一串标题和链接。见[搜索与时间](docs/web-search-time.md)。
- **多图与视频混发更自然**：单张图片异常不拖垮其他图片，批量识图被拒时逐张读取后整合；视频附件单独标记，不误报成识图配置错误，新媒体赶在错误提示前到达时重新合并。当前未提供视频画面/音轨时不会假装已经看过。见[识图与等待](docs/image-turn-taking.md)。
- **主动联系**：仅微信或仅QQ均可直接使用，无需关联；可选同人关联后共享随机计划、冷却、每日额度及等待回复状态，每次只选一个渠道。默认3～8小时随机间隔、每天最多2条、23:00～08:00免打扰。先在「模型与行为 → 主动联系」开启并选择联系人，默认关闭；未回复不连续催促。见[主动联系](docs/proactive-messages.md)。
- **跨端连续性**：已关联同人的近期私聊和主动发言可在另一端参考，未关联账号和群聊仍隔离；继续保留旧目录整套导入、独立本地数据与Windows输入焦点修复。

## 下载与平台

本次按确认**先发布 macOS arm64**；Windows构建机离线，v2.1.5尚无Windows安装包，不用旧文件冒充新版本。

| 平台 | 下载 | 发布大小 |
|---|---|---|
| macOS Apple Silicon | [WxBot-2.1.5-macOS-arm64.dmg](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.5/WxBot-2.1.5-macOS-arm64.dmg) | 约 162.2 MiB |
| v2.1.5 SHA256 | [SHA256SUMS.txt](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.5/SHA256SUMS.txt) | 只对应本次DMG |

**Windows最新安装包仍为v2.1.4**：[安装版 WxBot-2.1.4-Windows-x64-Setup.exe](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.4/WxBot-2.1.4-Windows-x64-Setup.exe) · [便携版 WxBot-2.1.4-Windows-x64-Portable.exe](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.4/WxBot-2.1.4-Windows-x64-Portable.exe) · [v2.1.4校验值](https://github.com/Sirius-Peng/wx-bot/releases/download/v2.1.4/SHA256SUMS.txt)。旧包不含上面的v2.1.5新功能；历史附件保持不变。

安装包自带运行时，无需 Python / Node.js。v2.1.5没有macOS Intel、Windows ARM64/32位或Linux桌面包；Linux可用源码/Docker，其他架构构建入口见[桌面说明](desktop/README.md)。发布包未配置代码签名，首次打开流程见同页。

## 升级与旧数据

macOS先从托盘退出旧应用，再将新App拖到「应用程序」替换。独立用户数据目录保持不变；建议先到「模型与行为 → 本地数据与备份」创建备份。Windows仍可使用含焦点修复的v2.1.4包覆盖安装。

v2.1.4起支持「模型与行为 → 导入旧版数据」：选择包含 `config.json` 的旧目录，预览并确认整套替换，准备导入后重启后台。导入前自动备份当前资料，旧目录不修改；保留配置、人格、记忆、上下文、聊天记录及普通文件，QQ Docker卷另行备份。详见[导入与恢复](docs/data-import.md)。

## 零基础开始

1. **安装并打开**应用，首次配置进入「上手引导」，选择微信、QQ 或两者。引导进度保存在本地，暂时跳过后可继续。
2. **配置模型**：默认地址 `https://api.deepseek.com`，API 模型名 `deepseek-flash`。在 [DeepSeek 开放平台](https://platform.deepseek.com/)登录，到 [API keys](https://platform.deepseek.com/api_keys) 创建 Key，检查余额后粘贴到引导中并点击「保存并测试模型」。测试会产生少量 API 用量；聊天网页版登录不等于已经配置 API。费用以[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)为准，不作固定预算或免费承诺。
3. **连接平台**：微信 iLink 使用手机微信扫码，无需独立网关或手填平台 Token。QQ 点首页「一键启动 QQ 网关」，按提示准备 Docker/NapCat；已保存账号可快速登录，首次或失效时扫码。二维码实际准备好且图片解码后才弹窗。
4. **完成并启动**机器人，发送「你好」或图片验证。已有登录会话尽量复用，是否需要重新确认由平台决定。

首次安装 Docker/启动服务可能需要系统确认；应用显示当前进度。网关的一键启动不会替用户完成手机确认。详细 URL、Token、镜像、快速登录账号及日志仍在「接入设置」；模型、识图、搜索、人格、行为与记忆各有自己的保存按钮。

## 当前功能

| 功能 | v2.1.5 实际行为（Windows旧包以v2.1.4为准） |
|---|---|
| 微信 | 本项目采用腾讯 iLink 私聊通道，长轮询收件；不提供微信群聊或 GeWe 通道 |
| QQ | NapCat / OneBot 11，私聊回复、群聊仅 @ 机器人时回复；反向 WebSocket 或 HTTP 上报 |
| 完整回复 | 等模型完成后发送一条完整消息；普通聊天合并多余换行，代码、列表等保留格式 |
| 连续输入 | 同一发言者有序合并文字和图片；新消息重置静默窗口，明确等待/结束信号影响作答时机 |
| 输入提示 | 真正开始处理后使用平台支持的原生正在输入状态；辅助失败不阻塞正文，不发送占位气泡 |
| 图片识别 | 下载/解密并归一化真实图片后传给视觉模型；独立视觉配置为空时复用主模型，主模型须支持图像输入 |
| 人格 | 默认弗洛洛，支持关系模式、场景少样本和自定义人格；提示词拟合，不是模型权重微调 |
| 长期记忆 | 事实、偏好、关系、事件与风格的后台积累；版本/有效期/来源、手动管理、持久化与失败重试 |
| 跨端连续性 | 微信/QQ 共用长期库；同一个人的账号需在「人格知识库」明确关联，不凭昵称猜测身份 |
| 上下文 | 私聊近期问答与后台摘要；关联同人的跨端近期互动可参考，群聊及未关联用户仍隔离 |
| 主动联系 | 仅微信或仅QQ均可；可选同人关联共享随机计划、冷却/额度，未回复不连续催促，夜间免打扰 |
| 联网与时间 | 默认按需 DeepSeek 辅助搜索，整合正文事实并复核，避免标题与链接堆砌；每轮注入当前时间，默认 `Asia/Shanghai` |
| 对话统计 | 高频词/话题气泡图、排行和筛选；本地 Jieba 精确分词及关键词规则，不调用 LLM |
| 数据与备份 | 独立本地目录、非覆盖迁移、校验过的 ZIP 备份；普通更新/卸载不主动删除用户数据 |

普通持续输入的基础窗口为 **2 秒**、最长等待 **10 秒**；拟人化规则会随句子完整度、图片和当前批次节奏调整，非固定逐条延迟。明确说「等我说完」默认最多等待 **120 秒**，「说完了」可尽快接答；窗口设 0 可关闭合并。已提交的回复不撤回，尚未提交的旧生成可被续话取消。

微信草稿、入站队列和 QQ 等待队列可持久恢复。微信发送检查 HTTP 与业务状态，并复用正文、`client_id` 和 `context_token` 重试；接口接受不等于客户端已读，进程在外部成功和本地确认之间中断仍可能重复。详情见[投递与输入状态](docs/ilink-delivery-check.md)。

## 管理界面

| 页面 | 内容 |
|---|---|
| 概览 | 运行/投递状态、最近消息、首页单按钮启动 QQ 网关 |
| 接入设置 | 通道、微信登录、QQ 扫码/快速登录、网关与推送状态、日志 |
| 人格知识库 | 记忆搜索、增删、历史/过期状态、积累队列/重试、微信↔QQ身份关联 |
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
python tools/smoke_ilink.py
python -m src.verify_qq
python tools/qr_check.py
python tools/check_docs.py
```

以上 `python` 应使用已安装项目依赖的虚拟环境。`check_docs.py` 为文档一致性检查，不调用模型或平台；详细验证范围见[测试矩阵](docs/validation.md)。

**v2.1.5发布验收**：本机macOS执行 **617项Python测试（1项大小写夹具跳过）**，9组Node回归通过，其中主动联系前端11项。冻结后端通过单端联系人、可选双端关联、分区保存与旧目录导入/重启检查；14个关键模块字节码、全部6份Web资源及267项只读DMG挂载内容与源码/构建App一致，三项二进制均为arm64。

本次未构建或原生测试Windows v2.1.5；Windows历史v2.1.4的533项原生回归、Portable启动及Setup候选包生命周期记录仍保留，不用旧结果替代本次验收。未使用真实账号或付费模型替代模拟接口。精确文件大小、摘要及边界见[当前发布清单](docs/release-v2.1.5.json)、[历史v2.1.4清单](docs/release-v2.1.4.json)和[测试矩阵](docs/validation.md)。

## 项目结构

```text
src/server.py, runtime.py       Web/API、运行时、账号与投递诊断
src/adapters/                  iLink 与 OneBot 11
src/core/conversation.py       有序图文与连续输入批次
src/core/turn_taking.py         本地轮流说话/等待规则
src/core/media.py               图片下载、解密与视觉输入
src/core/context.py, memory.py  人格/上下文/长期事实与身份关联
src/core/memory_learning.py     后台持久记忆抽取
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

此仓库提供本地文件形态的长期记忆与轻量词法/结构化召回，未接入向量数据库或托管记忆服务。下载包分别固定对应其发布标签：本次macOS为`v2.1.5`，Windows历史包为`v2.1.4`；主分支文档更新不会改动旧包或移动标签。
