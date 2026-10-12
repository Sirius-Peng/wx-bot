# 微信 AI 助手

接入大模型的聊天机器人：**微信私聊** + **QQ 私聊 / 群聊 @ 回复**。
自带 Web 管理界面与 Electron 桌面版，**双击即可运行**。

---

## 核心结论（先说重点）

1. **微信只走腾讯官方 iLink Bot API** —— 手机微信扫码即用，合规、零封号、官方维护。
2. **官方接口不支持微信群聊**（能力声明 `chatTypes: ["direct"]`），因此本项目
   **不再支持微信群聊**：非官方协议端（GeWe / WeChatPadPro 等）要么已停服，
   要么随时会坏，留着只会让用户对着一个坏通道排查。
3. **需要群聊就用 QQ** —— 走标准 OneBot 11（NapCat），私聊 + 群聊都支持，
   群聊里**只有 @ 机器人才回复**。
4. 若必须做微信群聊，建议改用**企业微信官方智能机器人**（官方支持群聊，零封号）。

---

## 三种使用方式

| 方式 | 适合谁 | 怎么开始 |
|---|---|---|
| **桌面应用** | 不想碰命令行 | [下载安装包](https://github.com/Sirius-Peng/wx-bot/releases/latest)，双击运行 |
| **一键脚本** | 会双击文件就行 | 双击 `start-mac.command` / `start-windows.bat` |
| **Docker / 命令行** | 有运维经验 | `docker compose up -d` 或 `python -m src.server` |

### 📦 下载安装包

前往 [**Releases**](https://github.com/Sirius-Peng/wx-bot/releases/latest) 页面下载：

| 平台 | 文件 |
|---|---|
| macOS（Apple Silicon） | `WxBot-1.3.0-macOS-arm64.dmg` |
| Windows | 需自行构建：`cd desktop && npm run build:backend && npm run pack:win` |

> **首次打开提示"无法验证开发者"**：应用未配置代码签名证书。
> 右键点应用 → 打开 → 再点"打开"即可（只需一次）。
> 若提示已损坏，执行 `xattr -cr "/Applications/微信AI助手.app"`

### 桌面应用版

原生窗口 + 系统托盘，**关掉窗口后机器人在后台继续静默运行**，可打包成单一安装包。

```bash
cd desktop
npm install
npm run icons            # 首次：生成图标
npm start                # 开发调试

npm run build:backend    # 打包 Python 引擎为单文件
npm run pack:mac         # 产出 .dmg
npm run pack:win         # 产出 NSIS 安装包 + 免安装 exe
```

详见 [`desktop/README.md`](desktop/README.md)。

---

## 快速开始（三步）

### 1️⃣ 启动

| 你的系统 | 操作 |
|---|---|
| **Windows** | 双击 `start-windows.bat` |
| **macOS** | 双击 `start-mac.command` |
| **Linux** | 终端执行 `./start.sh` |

> 首次运行会自动安装依赖（约 1–2 分钟），之后每次启动只需几秒。
> 浏览器会自动打开管理界面 `http://127.0.0.1:8000`。

### 2️⃣ 配置

在「**接入设置**」里勾选要用的通道（微信 / QQ，可同时开）：

| 通道 | 操作 |
|---|---|
| **微信** | 点「**获取登录二维码**」→ 手机微信扫码。**不需要网关、不需要任何 Token** |
| **QQ** | 点「**一键部署 QQ 网关**」→ 程序装好 NapCat、写好网络配置 → 页面直接出二维码 |

在「**模型与行为**」里：
- 填写大模型 API Key（推荐 DeepSeek）
- 设置机器人的**名称**与**人设**（会写进每一轮对话的系统提示词）
- 按需开关**人格知识库**（自动积累 / 回答时参考 / 抽取频率）

### 3️⃣ 启动

点右上角「**启动机器人**」，完成。

---

## 没有编程基础？先看这两条

**① 微信不需要装任何东西**

| 通道 | 依赖 | 怎么开始 |
|---|---|---|
| 微信 | **无** —— 腾讯官方 iLink 接口 | 点「获取登录二维码」，扫码即用 |
| QQ | NapCat（OneBot 11），程序自动部署 | 点「一键部署 QQ 网关」 |

只有 QQ 的一键部署需要 Docker。程序会自动：检测/安装 Docker → 启动服务 →
拉镜像 → 建容器 → 灌配置 → 回填地址 → 出登录二维码。

全程只有两处需要你动手（系统限制，无法自动化）：
- 安装 Docker 时**输入开机密码**
- 首次启动 Docker 时**同意许可协议**

界面会明确告诉你当前卡在哪一步、下一步做什么。

**② 需要一个「大模型 API Key」**
到 [DeepSeek 开放平台](https://platform.deepseek.com/) 注册并充值（10 元够用很久），
复制 API Key 填进「模型与行为」页面。

**③ 群聊不会刷屏**
QQ 群里机器人**只有被 @ 时才会回复**，其余消息一概不理。

---

## 界面

| 页面 | 作用 |
|---|---|
| **概览** | 运行状态、今日数据、最近动态 |
| **接入设置** | 通道勾选 + 微信扫码 / QQ 网关一键部署 |
| **人格知识库** | 机器人从对话中积累的对用户的理解，可手动增删 |
| **消息记录** | 每条消息的触发判定结果一目了然 |
| **模型与行为** | 大模型配置 + 机器人名称/人设 + 知识库开关 |

---

## 回复规则

规则只有两条，刻意保持简单：

```
消息到达
  │
  ├─ 私聊 ─────────────────────────→ 一律回复
  │
  ├─ QQ 群聊，@ 了机器人 ──────────→ 回复（默认同时 @ 回提问者）
  │
  └─ QQ 群聊，没 @ ────────────────→ 不回复
```

> **为什么不做「智能判断该不该插话」**：
> ① 用户 @ 了机器人却因为概率/评分没被回复，会直接认为机器人坏了；
> ② 判定要额外调一次模型，既慢又费 token；
> ③ 行为不可预测，用户无法解释"为什么这条回了那条没回"。
> 所以只保留最符合直觉的规则：**@ 了必回，没 @ 就不插话**。

---

## 两条通道的区别

| | 微信（官方 iLink） | QQ（OneBot 11） |
|---|---|---|
| 私聊 | ✅ | ✅ |
| 群聊 | ❌ 官方接口不支持 | ✅（仅 @ 时回复） |
| 合规性 | ✅ 腾讯官方 | ✅ OneBot 标准协议 |
| 封号风险 | **无** | 视网关实现而定 |
| 部署 | 扫码即用 | 程序一键部署 NapCat |
| 消息接收方式 | **拉**：程序长轮询 `getUpdates` | **推**：网关反向 WS / HTTP 上报 |

---

## 人格知识库

机器人会在对话中自动积累关于用户的信息（身份、偏好、关系、事件、风格），
下次回答时自动参考；相似信息**更新而非堆叠**，所以不会出现两条互相矛盾的记忆。

- 开关与抽取频率在「模型与行为」页
- 内容可在「人格知识库」页查看、搜索、手动添加、逐条删除
- 手动添加时对象留空 = **所有人通用**；填对方 ID 则只对那个人生效
- 抽取用的是「判定模型」，建议用免费的 GLM-4.7-Flash，这部分成本可降到 0

---

## 其他启动方式

### Docker 一键部署

```bash
docker compose up -d
# 打开 http://127.0.0.1:8000
```

主程序零外部依赖（配置存 JSON、消息存本地文件），**不需要数据库**。
微信通道不需要任何网关容器；要用 QQ 的一键部署，需按 `docker-compose.yml`
里的注释挂载 Docker socket。

### 命令行

```bash
pip install -r requirements.txt
python -m src.server
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WXBOT_PORT` | `8000` | 服务端口（被占用时自动顺延） |
| `WXBOT_HOST` | `127.0.0.1` | 监听地址 |
| `WXBOT_DATA_DIR` | `./data` | 数据目录（配置 + 消息记录） |
| `WXBOT_NO_BROWSER` | — | 设为 `1` 则不自动打开浏览器 |

---

## 自测（改完代码先跑这三条）

```bash
.venv/bin/python tools/smoke_ilink.py     # 微信链路端到端（假 iLink + 假模型，不联网）
.venv/bin/python -m src.verify_qq         # QQ 链路端到端（假 OneBot 网关）
.venv/bin/python tools/qr_check.py        # 二维码编码器正确性（离线）
```

`smoke_ilink.py` 会真的把「收消息 → 触发判定 → 拼人设 → 调模型 →
带 context_token 回复 → 写日志」整条链路跑一遍，并逐项断言 ——
这几个环节出问题都**不会报错，只会静默不工作**，所以必须靠测试兜住。

---

## 数据与隐私

- 所有配置和消息记录都保存在本机 `data/` 目录，**不上传任何服务器**
- 配置文件 `data/config.json` 里含 API Key，权限已收紧为仅本人可读
- 微信登录凭证存在 `data/ilink/account.json`，同样是 0600 权限
- 想备份或迁移，直接拷贝 `data/` 目录即可

---

## 项目结构

```
src/                        Python 引擎
  server.py                 Web 服务（管理界面 + API + OneBot 上报入口）
  runtime.py                机器人运行时（启停 / 状态 / 消息日志 / 扫码登录 / iLink 长轮询）
  config_store.py           配置持久化（原子写入 + 密钥掩码 + 旧配置迁移）
  orchestrator.py           编排层，串联完整链路
  store_memory.py           会话存储
  verify_qq.py              QQ 链路端到端验证脚本
  web/                      管理界面（零构建步骤：HTML + CSS + 原生 JS）
  core/
    types.py                统一消息结构（换协议不改业务的关键）
    trigger.py              回复判定（私聊必回 / 群聊仅 @）
    context.py              上下文装配 + 人设拼装
    memory.py               人格知识库（抽取 / 融合 / 召回 / 注入）
    llm.py                  大模型网关（路由 / 降级 / 成本统计）
    sender.py               拟人化发送（限流 / 延迟 / 分段）
    qr.py                   纯 Python 二维码编码（离线出图）
    docker.py               Docker 环境检测与 NapCat 一键部署
  adapters/
    base.py                 适配器契约
    ilink.py                腾讯官方 iLink Bot API（仅私聊，长轮询收消息）
    onebot.py               OneBot 11（QQ，私聊 + 群聊）

tools/
  smoke_ilink.py            微信链路端到端冒烟测试
  qr_check.py               二维码编码器自检

desktop/                    Electron 桌面版
  src/main/                 主进程（窗口 / 托盘 / 后端进程 / IPC / 路径）
  src/preload/              安全桥接
  backend/entry.py          PyInstaller 打包入口
  scripts/                  后端打包 + 图标生成
  electron-builder.yml      打包配置

start-mac.command           一键启动脚本
start-windows.bat
start.sh
docker-compose.yml / Dockerfile
requirements.txt
```

**架构要点**：所有协议适配器都归一化到同一个 `WxMessage` 结构，因此换协议不改业务逻辑；
桌面版把 Python 引擎作为 sidecar 二进制，前端 UI 与浏览器版完全复用。

---

## ⚠️ 使用须知

- 微信通道走**腾讯官方接口**，合规、零封号，但腾讯保留随时调整或终止服务的权利。
- QQ 群聊能力由第三方网关（NapCat 等）提供，请遵守对应平台的使用条款。
- 请勿用于营销群发、骚扰等违规用途。
- 请遵守《生成式人工智能服务管理暂行办法》，使用已备案的国内大模型服务。
