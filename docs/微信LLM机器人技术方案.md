# 微信 LLM 对话机器人技术方案

> 版本：v1.0 ｜ 日期：2026-10-12
> 目标：设计一个接入 LLM API 的微信对话机器人，支持**一对一私聊**与**群聊中按内容判断是否插话**

---

## 0. 结论速览（TL;DR）

| 问题 | 结论 |
|---|---|
| 个人微信有官方 API 吗？ | **有，但仅限私聊**。腾讯 2026-03 通过 OpenClaw 开放了官方 **iLink Bot API**（微信 ClawBot），合规、零封号。但源码能力声明为 `chatTypes: ["direct"]`——**不支持群聊** |
| 群聊有官方方案吗？ | **没有**。企业微信官方机器人作用域仅限内部通讯录成员；个人微信官方 iLink 协议不支持群聊。群聊只能走协议方案 |
| 推荐技术路径 | **混合架构**：私聊走官方 iLink Bot API（合规），群聊走 iPad 协议（有封禁风险）。把风险压缩到只存在于群聊场景 |
| 群聊触发策略 | **默认「仅 @ 机器人时回复」**：私聊一律回复，群聊只有 @ 了才回。可选「智能插话」：五层漏斗（启发式 → 小模型判定 → 冷却配额），约 1~2% 消息被回复 |
| 持久化 | **三层存储**：Redis（热，上下文窗口）+ PostgreSQL（温，归档/配置）+ pgvector（冷，长期记忆/RAG） |
| 最省钱的 LLM 组合 | 判定用小模型（GLM-4.7-Flash 免费 / qwen-flash），回答用 DeepSeek-V4-Flash（¥1.01/¥2.02 每百万 token，缓存命中省 98%） |

**一句话总结**：微信生态的官方能力存在一条**明确的分界线**——**私聊已开放、群聊未开放**。所以最优解不是二选一，而是**混合架构**：私聊全部走官方 iLink Bot API（合规、零封号、官方维护），群聊退回 iPad 协议（能力完整但需承担封禁风险）。这样把合规风险压缩到最小范围。

> ⚠️ 本文档 v1.0 曾判断"个人微信无官方 API"。该结论已于 v1.1 修正，依据是对 `@tencent-weixin/openclaw-weixin@2.4.10` 源码的逐行核验与真实接口联调。详见第 2.2 节 A5 与 [`官方iLink-Bot-API-接入说明.md`](官方iLink-Bot-API-接入说明.md)。


---

## 1. 需求拆解与关键约束

### 1.1 功能需求

| 编号 | 需求 | 隐含技术挑战 |
|---|---|---|
| R1 | 一对一私聊对话互动 | 会话隔离、多轮上下文、并发串行化 |
| R2 | 群聊中存在，并按消息内容判断何时回复 | **触发判定**（核心难点）、群上下文建模、发言频率控制 |

### 1.2 关键约束（决定了整个方案形态）

1. **协议约束**：个人微信的**私聊**已有官方 API（iLink Bot API，见 2.2 节 A5），合规且零封号；但**群聊官方未开放**。群聊自动化只能依赖逆向协议，存在**账号风控风险**，且需随微信版本更新持续适配。这是本方案唯一需要承担风险的环节。
2. **合规约束**：企业微信官方能力与个人微信生态之间有一道**硬隔离墙**——官方机器人无法触达个人微信好友与外部客户群。
3. **体验约束**：微信不是为机器人设计的 IM。没有"正在输入"指示器、没有原生流式消息、单条消息长度有限、发送频率受风控限制。
4. **成本约束**：群聊消息量大，若每条消息都调用 LLM，成本会失控。**必须做分层过滤**。

> 这四条约束中，第 1、2 条是"选型问题"，第 3、4 条是"工程问题"。本方案先解决选型，再解决工程。

---

## 2. 微信侧接入方案选型

### 2.1 候选方案全景

```
微信机器人接入方案
├── A. 官方合规路径
│   ├── A1. 企业微信 · 智能机器人（API 模式）
│   ├── A2. 企业微信 · 群机器人 Webhook
│   ├── A3. 微信客服（kf.weixin.qq.com）
│   ├── A4. 微信公众号 · 客服消息
│   └── A5. 个人微信 · 官方 iLink Bot API（微信 ClawBot）★ 2026-03 新增
├── B. 协议逆向路径（个人微信 / 企业微信）
│   ├── B1. iPad 协议（Gewechat / WeChatPadPro / 商业 API 网关）
│   ├── B2. PC Hook 注入（WeChatFerry / wechaty-puppet-xp）
│   ├── B3. Web 协议（wechaty-puppet-wechat4u / ItChat）
│   └── B4. 商业托管 Puppet（wechaty-puppet-padlocal）
└── C. 应用层框架（非接入方案，是可复用的上层）
    ├── C1. LangBot（LLM 原生 IM 机器人平台）
    ├── C2. AstrBot（多平台 LLM 机器人）
    └── C3. chatgpt-on-wechat（多平台对话机器人）
```

### 2.2 逐项对比

#### A 类：官方路径

| 方案 | 一对一个人微信 | 群聊 | 接收消息 | 发送消息 | 门槛 | 风险 |
|---|---|---|---|---|---|---|
| **A5 官方 iLink Bot API** ★ | ✅ **完全支持** | ❌ **不支持**（`chatTypes:["direct"]`） | ✅ 长轮询 getupdates | ✅ sendmessage | 扫码登录 | **无** |
| **A1 企微智能机器人** | ❌ 仅企微内部成员 | ⚠️ 仅内部群，且**只收 @ 消息** | ✅ URL 回调 / 长连接 | ✅ 被动回复 | 企业认证 + **超管权限** | 无 |
| **A2 企微群 Webhook** | ❌ | ⚠️ 仅内部群，**只能发不能收** | ❌ | ✅ 20 条/分钟 | 群成员即可 | 无 |
| **A3 微信客服** | ⚠️ 需用户**主动发起**会话 | ⚠️ 可发名片到群，用户点击后转私聊 | ✅ sync_msg 拉取 | ✅ 48h 窗口内 | 企业主体 | 无 |
| **A4 公众号客服消息** | ⚠️ 需先关注公众号 | ❌ 不支持群聊 | ✅ 服务器推送 | ✅ 48h 窗口内 | 认证服务号 | 无 |

**A 类小结**：
- **A5 是唯一能直连"个人微信好友私聊"的官方方案**，且合规、零封号——这是 2026 年最重要的变化。但**它不支持群聊**。
- A1 是最接近"群聊机器人"的官方能力，但**进不了外部客户群**——外部微信用户 @ 它没有任何回调。
- A3/A4 只能做"一对一客服"，且必须用户主动发起。

> **结论：官方路径可以覆盖 R1（私聊），无法覆盖 R2（群聊）。**

#### A5 详解：官方 iLink Bot API（微信 ClawBot）

**背景**：腾讯 2026-03 通过 [OpenClaw](https://docs.openclaw.ai)（AI Gateway 框架）开放了个人微信的 Bot 能力，官方名称「微信 ClawBot 插件功能」，底层协议 **iLink**，接入域名 `ilinkai.weixin.qq.com`。腾讯为此发布了专项条款《微信ClawBot功能使用条款》。

**证据链**（本文档已逐项独立核验，非文档自述）：

| 核验项 | 结果 |
|---|---|
| npm 包 `@tencent-weixin/openclaw-weixin` | ✅ 存在，author=Tencent，MIT，维护者邮箱均为 `@tencent.com` |
| 版本与活跃度 | ✅ 26 个版本，首版 2026-03-21，最新 **2.4.10**（2026-10-10），月下载 9.9 万 |
| 官方仓库 | ✅ `github.com/Tencent/openclaw-weixin`，935 star，2026-10-11 仍在提交 |
| 域名归属 | ✅ `ilinkai.weixin.qq.com` CNAME → `aewebpodproxy.weixin.qq.com`（腾讯自有） |
| 接口可用性 | ✅ 实测 `GET /ilink/bot/get_bot_qrcode?bot_type=3` 返回 `ret:0` + 真实二维码 |
| **群聊支持** | ❌ `capabilities.chatTypes = ["direct"]`；`inbound.ts` 硬编码 `ChatType:"direct"`；CHANGELOG 26 版零次提及群聊；源码零群聊实现 |

**协议要点**：

```javascript
// 请求头（固定套路）
{
  "Content-Type": "application/json",
  "AuthorizationType": "ilink_bot_token",
  "X-WECHAT-UIN": base64(String(randomUint32())),  // 每次随机，防重放
  "Authorization": `Bearer ${bot_token}`
}
```

| Endpoint | Method | 功能 |
|---|---|---|
| `/ilink/bot/get_bot_qrcode?bot_type=3` | GET | 获取登录二维码 |
| `/ilink/bot/get_qrcode_status?qrcode=xxx` | GET | 轮询扫码状态 |
| `/ilink/bot/getupdates` | POST | **长轮询收消息**（服务端 hold ≤35s） |
| `/ilink/bot/sendmessage` | POST | 发送消息 |
| `/ilink/bot/sendtyping` | POST | **发送"正在输入"状态** |
| `/ilink/bot/getconfig` | POST | 获取 typing_ticket |
| `/ilink/bot/getuploadurl` | POST | 获取 CDN 预签名上传地址 |
| `/ilink/bot/msg/notifystart` / `notifystop` | POST | 上报本端启停 |

**两个关键细节**：

1. **`context_token` 是回复的必需字段**。每条入站消息都带 `context_token`，回复时必须原样带回，否则消息关联不到正确会话窗口。这是最容易踩的坑。
2. **`get_updates_buf` 是游标**。类似数据库 cursor，必须每轮更新，否则会重复收到消息。

**官方协议相对协议方案的三个体验优势**：
- ✅ **支持"正在输入"状态**（`sendtyping`）——协议方案做不到，长回答等待体验显著改善
- ✅ **消息状态机**（`NEW`/`GENERATING`/`FINISH`）——协议层预留了渐进式更新能力（当前插件只发 `FINISH`）
- ✅ **语音自带转文字**（`voice_item.text`）——省掉一层 ASR

**必须知道的约束**（来自官方条款）：
- 腾讯只是"管道"，不存储你的输入与输出，**不提供 AI 服务**
- 腾讯**保留控制权**：可决定你可连接的第三方 AI 服务类型、范围、收发频率，可拦截内容、可终止服务（条款 4.7、7.2）
- 腾讯**收集 IP、操作记录、设备信息**用于安全审计（条款 5.3）
- **禁止**利用本功能绕过/破解微信技术保护措施（条款 4.6）
- ⇒ **不应把核心业务完全依赖这套 API，必须有降级方案**

**环境要求**：Node.js `>=22.13.0`，OpenClaw `>=2026.5.12`。

#### B 类：协议路径

| 方案 | 原理 | 部署 | 能力完整度 | 稳定性 | 封号风险 | 成本 |
|---|---|---|---|---|---|---|
| **B1 iPad 协议** | 逆向 iPad 客户端 TCP+Protobuf 私有协议，纯代码模拟设备登录 | **Linux 云端**，7×24 常驻 | ⭐⭐⭐⭐⭐ 全类型消息、群聊、@、朋友圈 | 高（大版本需适配） | **中**（可控） | 开源免费 / 商业网关按量 |
| **B2 PC Hook** | DLL 注入 Windows 微信进程，劫持收发函数 | 必须常驻 **Windows 客户端** | ⭐⭐⭐⭐⭐ 最强（可读数据库、加好友、群管理） | 中（微信更新即失效） | **高**（2025-04 封号风暴中 WCF 用户"几乎无一幸免"） | 免费 |
| **B3 Web 协议** | 模拟网页版微信 | 任意 | ⭐⭐ 仅基础收发 | **已废弃** | — | 免费 |
| **B4 PadLocal** | 商业托管 iPad 协议 | 云端 | ⭐⭐⭐⭐ | 高 | 低-中 | **约 ¥200/月/号** |

**B 类小结**：
- **B3 直接淘汰**：2017 年后注册的微信号无法再登录网页版微信。
- **B2 仅用于本地测试**：能力最强但封号率最高，且强绑定 Windows，不可用于生产。
- **B4 是"花钱买省心"**：wechaty 生态最成熟的商用 puppet，适合预算充足、追求稳定的团队。
- **B1 是性价比最优解**：可云端部署、不占本地设备、并发能力好、开源免费。

#### C 类：应用层框架（可作为上层复用）

| 框架 | 定位 | 微信适配器 | 是否适合本方案 |
|---|---|---|---|
| **LangBot** | LLM 原生 IM 机器人平台，自带 WebUI、插件系统、Dify/Coze 集成 | 个人微信（OpenClaw / WeChatPadPro / Gewechat）、企微、公众号等 | ✅ **强烈推荐作为 PoC 起点** |
| **AstrBot** | 松耦合异步多平台 LLM 机器人，插件丰富 | 多平台 | ✅ 可选 |
| **chatgpt-on-wechat** | 老牌多平台对话机器人 | 微信（多协议）、企微、公众号、飞书、钉钉 | ⚠️ 架构较老，扩展性一般 |

### 2.3 选型结论：混合架构

> **推荐路径：私聊走 A5（官方 iLink Bot API），群聊走 B1（iPad 协议）**

```
                    ┌──────────────────────────────┐
                    │      混合适配器路由器         │
                    │  HybridAdapterRouter          │
                    └───────┬──────────────┬────────┘
                            │              │
              chat_type=private        chat_type=group
                            │              │
                            ▼              ▼
              ┌──────────────────┐  ┌──────────────────┐
              │ A5 官方 iLink     │  │ B1 iPad 协议      │
              │ 合规 · 零封号     │  │ 能力完整 · 有风险 │
              │ ✅ 私聊           │  │ ✅ 私聊 + 群聊    │
              │ ❌ 群聊           │  │                  │
              └──────────────────┘  └──────────────────┘
                            │              │
                            └──────┬───────┘
                                   ▼
                        统一的 WxMessage DTO
                                   ▼
                    触发判定 / 上下文 / LLM / 发送器
                        （完全复用，零改动）
```

**为什么这样分工**：

1. **私聊没有理由再冒风险**。A5 是官方协议，合规、零封号、腾讯自己维护，且额外提供"正在输入"状态、语音转文字、消息状态机等协议方案做不到的能力。私聊场景**必须**走 A5。
2. **群聊官方确实没有**。已核验：`chatTypes: ["direct"]`、`ChatType` 硬编码、CHANGELOG 零次提及群聊。所以群聊只能退回 B1。
3. **风险被压缩到最小范围**。只有群聊场景需要承担封禁风险，私聊完全干净。且群聊可以使用小号、独立账号。
4. **适配器抽象让这个分工几乎零成本**。所有适配器归一化到同一个 `WxMessage`，上层触发判定 / 上下文 / LLM / 发送器完全复用。参考实现见 `src/adapters/hybrid.py`。

**代码层面**：适配器通过 `supports_private` / `supports_group` 声明能力，路由器据此分流：

```python
class ILinkAdapter(BaseAdapter):
    supports_private = True
    supports_group   = False   # 对应官方 capabilities.chatTypes = ["direct"]

class GewechatAdapter(BaseAdapter):
    supports_private = True
    supports_group   = True    # iPad 协议支持群聊
```

**降级与容灾**（官方条款明确腾讯可随时限速/终止，必须有备份）：

| 场景 | 主方案 | 降级方案 |
|---|---|---|
| 私聊 | A5 官方 iLink | B1 iPad 协议（协议层能力等价） |
| 群聊 | B1 iPad 协议 | 无（官方无替代，只能接受能力缺失） |
| A5 被限速 | 切换到 B1 | — |
| 腾讯终止 A5 | 全量切 B1 | 需提前做好双通道并行 |

**若业务可收敛到企业内部**：
> 如果"用户"实际上是内部员工、需求本质是内部知识问答，则**直接改用 A1（企业微信智能机器人）**——零封号风险、官方长期支持、进群 @ 即可对话，且**官方支持群聊**。这是唯一"睡得着觉"且满足 R2 的合规方案。

**必须提前知会的风险**：B1 属于非官方协议调用，存在账号被风控的可能。**请勿使用主号**，建议使用专门的机器人号（实名、有日常使用记录的老号）。私聊已无此风险。


---

## 3. 总体架构

### 3.1 分层架构

```
┌──────────────────────────────────────────────────────────────────┐
│  微信侧                                                           │
│  个人微信(iPad协议)  /  企业微信  /  微信群                        │
└───────────────────────────┬──────────────────────────────────────┘
                            │ ① 消息回调 / 长连接
┌───────────────────────────▼──────────────────────────────────────┐
│  接入层 · Adapter Layer                                           │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐    │
│  │ Gewechat   │ │ WeChatPad  │ │ WCF(测试)  │ │ 企微智能   │    │
│  │ Adapter    │ │ Adapter    │ │ Adapter    │ │ 机器人     │    │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘    │
│  统一接口：on_message / send_text / send_image / get_member      │
└───────────────────────────┬──────────────────────────────────────┘
                            │ ② 标准化 Message DTO
┌───────────────────────────▼──────────────────────────────────────┐
│  消息总线 · Redis Stream（削峰 / 幂等 / 重放 / 背压）              │
└───────────────────────────┬──────────────────────────────────────┘
                            │ ③ 消费
┌───────────────────────────▼──────────────────────────────────────┐
│  编排层 · Orchestrator                                            │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐     │
│  │ TriggerRouter  │→ │ ContextManager │→ │  LLMGateway    │     │
│  │ 触发判定(5层)  │  │ 上下文装配/压缩 │  │ 路由/降级/缓存 │     │
│  └────────────────┘  └────────────────┘  └────────────────┘     │
│         │                                         │              │
│         └──────────→ ReplyPlanner（拟人化）←──────┘              │
│                       分段/延迟/概率                              │
└───────────────────────────┬──────────────────────────────────────┘
                            │ ④ 出站队列
┌───────────────────────────▼──────────────────────────────────────┐
│  存储层                                                           │
│  Redis（热：上下文窗口/冷却/限流）                                 │
│  PostgreSQL（温：消息归档/会话元数据/群配置/用户画像）              │
│  pgvector（冷：长期记忆 / 知识库 RAG）                             │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 核心链路时序

```
用户消息 → Adapter → 标准化 DTO → Redis Stream
                                      ↓
                              Orchestrator 消费
                                      ↓
                    ┌─────────────────┴──────────────────┐
                    ↓                                    ↓
            幂等检查(msg_id)                     加载群/会话配置
                    ↓                                    ↓
                    └─────────────────┬──────────────────┘
                                      ↓
                            TriggerRouter.decide()
                                      ↓
                        ┌─────────────┴─────────────┐
                     不回复                        回复
                        ↓                           ↓
                  写归档后结束              ContextManager.build()
                                                        ↓
                                              LLMGateway.chat()（流式）
                                                        ↓
                                              ReplyPlanner（分段+延迟）
                                                        ↓
                                            Outbox → 限流器 → Adapter.send()
                                                        ↓
                                              写归档 + 更新上下文
```

### 3.3 标准化消息 DTO

所有 Adapter 必须归一化到同一结构，这是"换协议不改业务"的关键。

```python
@dataclass
class WxMessage:
    msg_id: str              # 全局唯一，用于幂等
    platform: str            # gewechat | wechatpad | wcf | wecom_aibot
    chat_type: str           # "private" | "group"
    chat_id: str             # 私聊=对方wxid；群聊=群id(roomid)
    sender_id: str           # 发送者 wxid
    sender_name: str         # 发送者昵称（群聊上下文需要）
    bot_id: str              # 机器人自己的 wxid（用于判断 @自己）
    content: str             # 文本内容（已剥离 @ 前缀）
    raw_content: str         # 原始文本（保留 @ 信息）
    msg_type: str            # text | image | voice | file | video | system
    mentions: list[str]      # 被 @ 的 wxid 列表
    quote_msg_id: str | None # 引用回复的目标消息 id
    timestamp: int           # 毫秒时间戳
    is_at_me: bool           # 是否 @ 了机器人
```

---

## 4. 消息接收与发送机制

### 4.1 适配器抽象

```python
class BaseAdapter(ABC):
    """所有协议适配器的统一契约"""

    @abstractmethod
    async def start(self) -> None: ...
        # 启动：iPad协议→注册回调URL；WCF→连接gRPC端口；企微→校验URL

    @abstractmethod
    async def on_message(self, handler: Callable[[WxMessage], Awaitable[None]]) -> None: ...
        # 注册消息处理器，收到消息后归一化为 WxMessage 并回调

    @abstractmethod
    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str: ...
        # 返回 msg_id

    @abstractmethod
    async def send_image(self, chat_id: str, path_or_url: str) -> str: ...

    @abstractmethod
    async def get_group_member(self, chat_id: str, user_id: str) -> dict: ...
        # 获取群成员昵称等信息，用于上下文标注

    @abstractmethod
    async def is_healthy(self) -> bool: ...
        # 健康检查：登录态是否有效
```

**各适配器实现要点**：

| Adapter | 接收方式 | 发送方式 | 注意事项 |
|---|---|---|---|
| Gewechat | HTTP 回调（POST 到本服务） | HTTP POST `/sendText` | 需配置公网回调地址；注意回调重试导致重复，必须幂等 |
| WeChatPadPro | HTTP 回调 | HTTP POST | 同上；注意异地登录告警 |
| WCF | gRPC 长连接 + 消息推送 | gRPC 调用 | 仅 Windows；需处理客户端崩溃重连 |
| 企微智能机器人 | URL 回调（AES 加密） | 被动回复（加密 XML/JSON） | 回复必须在 **5 秒内**返回，长任务需先 ACK 后异步补发 |

### 4.2 接收链路：三个必须解决的问题

**① 幂等去重**

协议网关回调通常有重试机制，同一 `msg_id` 可能到达多次。

```python
async def dedup(msg_id: str) -> bool:
    """返回 True 表示首次到达，False 表示重复"""
    key = f"dedup:{msg_id}"
    # SET NX EX 原子操作
    ok = await redis.set(key, "1", nx=True, ex=3600)
    return bool(ok)
```

**② 顺序与乱序**

微信消息在高并发下可能乱序到达。策略：**按 `chat_id` 做分区有序**——同一会话内按时间戳排序处理，不同会话完全并行。

**③ 背压与削峰**

消息洪峰（如被拉进一个 500 人大群）会打爆 LLM 调用。策略：
- 消息总线（Redis Stream）天然削峰，`XADD` 快、消费可控
- 消费者组按 `chat_id` 哈希分区，保证同会话串行
- 队列积压超阈值时，**自动降级**：只处理 `is_at_me=True` 的消息，其余只归档不回复

### 4.3 发送链路：拟人化与限流

微信对机器人行为的风控特征非常明确——**固定间隔、高频、大段文本**。发送器必须做三件事：

```python
class RateLimitedSender:
    # 1. 令牌桶限流：单账号全局 + 单会话
    #    全局：≤ 20 条/分钟；单群：≤ 2 条/分钟（关键！）
    # 2. 随机延迟：模拟真人打字
    #    延迟 = 基础(1.5s) + 0.03s × 文本长度 + random(0, 2.5s)
    # 3. 长文本分段：微信单条消息有长度上限，按语义切分后分段发送
    async def send(self, chat_id: str, text: str):
        await self.bucket_global.acquire()
        await self.bucket_chat(chat_id).acquire()
        for seg in split_by_semantics(text, max_len=800):
            await asyncio.sleep(self.human_delay(seg))
            await self.adapter.send_text(chat_id, seg)
```

**风控红线（务必遵守）**：
- 单群发言 ≤ 2 条/分钟，杜绝营销式刷屏
- 禁止主动向陌生人群发（只回复，不主动触达）
- 使用实名、有正常使用记录的老号，不用批量注册的新号
- 固定出口 IP，避免异地登录告警
- 深夜时段（如 00:00–07:00）自动静默

---

## 5. LLM API 调用与上下文管理

### 5.1 LLM 网关设计

**统一走 OpenAI 兼容协议**——DeepSeek、通义千问、智谱 GLM 全部兼容，切换成本几乎为零。

```python
class LLMGateway:
    """
    职责：
      1. 多供应商路由（主模型 / 兜底模型 / 判定模型）
      2. 自动降级（主模型 5xx / 超时 → 兜底模型）
      3. 重试与超时控制（首次 token 超时 8s，整体超时 60s）
      4. 缓存（system prompt 固定前缀，命中缓存省 98%）
      5. 成本统计与配额（按 chat_id / 按天）
      6. 内容安全（敏感词前置过滤 + 模型侧兜底）
    """
```

**关键配置**：

```python
PROVIDERS = {
    "judge": {                       # 群聊触发判定用，要求快 + 便宜
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.7-flash",    # 免费，200K 上下文
        "timeout": 3.0,              # 判定必须快，超时即放弃（不回复）
        "max_tokens": 16,            # 只需要输出 yes/no
    },
    "chat_primary": {                # 正式回答
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",    # V4-Flash：¥1.01 / ¥2.02 每百万 token
        "timeout": 60.0,
    },
    "chat_fallback": {               # 降级
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",        # ¥0.8 / ¥2.0
        "timeout": 60.0,
    },
}
```

### 5.2 模型选型对比（2026-07 实测价，元/百万 Token）

| 模型 | 上下文 | 输入 | 输出 | 适用场景 |
|---|---|---|---|---|
| **GLM-4.7-Flash** | 200K | **免费** | **免费** | ✅ 群聊触发判定、试错阶段 |
| **DeepSeek-V4-Flash** | 1M | ¥1.01 | ¥2.02 | ✅ **主对话模型首选**（缓存命中省 98%） |
| DeepSeek-V4-Pro | 1M | ¥3.13 | ¥6.26 | 复杂推理、代码 |
| qwen3-max | 256K | ¥2.5 | ¥10 | 阿里云生态、长文 |
| qwen-plus | — | ¥0.8 | ¥2.0 | 降级兜底 |
| GLM-5.2 | 1M | ¥8 | ¥28 | 长文本深度理解（贵） |

**选型建议**：**判定用 GLM-4.7-Flash（免费），回答用 DeepSeek-V4-Flash**。这一组合把成本压到极致。

> **省钱技巧**：DeepSeek 支持上下文缓存，把固定的 system prompt 放在 messages 最前面、内容完全一致，可命中缓存，输入价格从 ¥1.01 降到 ¥0.02（省 98%）。多轮对话场景实际成本可能比标价低一个数量级。

### 5.3 上下文分层与压缩

**核心设计：四层上下文结构**

```
┌─────────────────────────────────────────────┐
│ L0 System Prompt（永不变）                   │  ← 固定前缀，命中缓存
│    人设 + 行为准则 + 输出格式约束            │
├─────────────────────────────────────────────┤
│ L1 长期记忆（滚动摘要）                      │  ← 超窗口时由 LLM 压缩生成
│    "用户此前询问过订单 A123 的物流状态"       │
├─────────────────────────────────────────────┤
│ L2 短期窗口（最近 N 轮）                     │  ← Redis 热数据
│    完整问答对，按 token 预算动态截断          │
├─────────────────────────────────────────────┤
│ L3 当前消息 + RAG 检索结果                   │  ← 实时拼装
└─────────────────────────────────────────────┘
```

**Token 预算分配（以 8K 预算为例）**：

| 层 | 预算 | 说明 |
|---|---|---|
| L0 System Prompt | 500 | 固定 |
| L1 长期摘要 | 500 | 超限则再压缩 |
| L2 短期窗口 | 5000 | 从最新往前装，装不下为止 |
| L3 当前消息 + RAG | 1500 | RAG 结果按相关性截断 |
| 输出预留 | 500 | `max_tokens` |

**压缩策略**：当 L2 累计 token 超过 5000 时，触发**滚动摘要**——把最早的 3 轮对话交给判定模型（免费）压缩成一句话，追加到 L1，并从 L2 移除。

```python
class ContextManager:
    async def build(self, msg: WxMessage) -> list[dict]:
        ctx_key = self._key(msg)
        history = await self.store.get_recent(ctx_key)   # L2
        summary = await self.store.get_summary(ctx_key)  # L1

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if summary:
            messages.append({"role": "system", "content": f"[历史摘要] {summary}"})
        messages += self._fit_budget(history, budget=5000)
        messages.append({"role": "user", "content": self._render(msg)})
        return messages

    async def append(self, msg: WxMessage, reply: str):
        ctx_key = self._key(msg)
        await self.store.push(ctx_key, msg, reply)
        if await self.store.token_count(ctx_key) > 5000:
            await self._compact(ctx_key)   # 触发滚动摘要
```

**群聊上下文特殊处理**：群聊是**多参与者共享上下文**，必须在每条消息前标注发言人，否则 LLM 无法区分谁在说话。

```python
def _render(self, msg: WxMessage) -> str:
    if msg.chat_type == "group":
        return f"[{msg.sender_name}]: {msg.content}"
    return msg.content
```

**隐私红线**：群聊上下文**默认不写入长期记忆**（L1），也不做跨群用户画像关联。仅保留短期窗口（TTL 24h），避免群成员信息被沉淀。如确需长期记忆，必须显式脱敏并告知群成员。

### 5.4 流式输出在微信里的落地

微信**没有原生流式消息**。三种落地方式：

| 方案 | 实现 | 体验 | 风险 |
|---|---|---|---|
| **整段发送** | 等 LLM 全部生成完，一次性发出 | 简单可靠 | 长回答等待久，用户以为机器人死了 |
| **分段发送**（推荐） | 按标点/语义切分，每段生成完就发 | 接近流式，体验好 | 多条消息可能被风控注意 |
| **占位符编辑** | 先发"思考中…"，再撤回/编辑 | 体验最好 | 部分协议不支持编辑消息 |

**推荐做法**：**分段发送 + 首段快速响应**。

```python
async def stream_reply(chat_id: str, stream):
    buffer, first = "", True
    async for chunk in stream:
        buffer += chunk
        # 首段阈值低（快速给反馈），后续按句子边界切
        threshold = 20 if first else 80
        if len(buffer) >= threshold and ends_at_boundary(buffer):
            await sender.send(chat_id, buffer.strip())
            buffer, first = "", False
    if buffer.strip():
        await sender.send(chat_id, buffer.strip())
```

### 5.5 并发与一致性

- **同会话串行**：同一 `chat_id` 的请求必须串行处理，否则会出现"上下文竞争"（两条消息同时读到同一份历史，各自追加，后者覆盖前者）。实现：Redis 分布式锁 `lock:chat:{chat_id}`。
- **跨会话并行**：不同 `chat_id` 完全并行，Worker 数可水平扩展。
- **超时保护**：LLM 调用超时（60s）则回复"稍等一下，我重新想想～"，并释放锁。

---

## 6. 群聊触发回复判断策略（核心）

### 6.1 两种策略

系统提供两种群聊策略，**默认使用「仅 @ 机器人」**，可在管理界面一键切换。

| | **仅 @ 机器人时回复**（默认） | **智能判断（自动找时机）** |
|---|---|---|
| 私聊 | ✅ 一律回复 | ✅ 一律回复 |
| 群聊 @ 机器人 | ✅ 回复 | ✅ 回复 |
| 群聊未 @ | ❌ **一律不回复** | ⚠️ 可能被漏斗捞起来 |
| 启发式打分 | ❌ 不启用 | ✅ 启用 |
| 小模型判定 | ❌ 不启用 | ✅ 启用 |
| 唤醒词 | ❌ 不启用 | ✅ 启用 |
| 回复概率闸门 | ❌ 不适用（@ 了必回） | ✅ 默认 0.7 |
| 默认冷却 | **0 秒（不限速）** | 60 秒 |
| 适用场景 | 绝大多数场景，最不打扰群友 | 希望机器人更活跃、能主动参与讨论 |

> **为什么默认选「仅 @」**：用户 @ 了机器人却因为概率闸门或冷却没被回复，体验非常差，会让人以为机器人坏了。因此该模式下默认不做任何限速与概率控制 —— **@ 了必回**。想限速可在界面里自行设置。

**仅 @ 模式的决策流程**：

```
消息到达
  │
  ├─ 私聊？ ────────────────────────→ 回复
  │
  ├─ 群开关关闭 / 机器人自身消息？ ──→ 不回复
  │
  ├─ @ 了机器人？ ──┬─ 是 ─→ 检查冷却/配额 ─→ 回复
  │                 └─ 否 ─→ 不回复（到此为止）
  │
  └─ 结束
```

> 实现上有一个容易踩的坑：`@ 机器人`、`引用机器人`、`唤醒词`、`关键词` 是四条**不同的**放行入口。如果只在 `@` 那条路上做冷却检查，其他入口就绕过去了，有人刷唤醒词就能刷屏。因此所有"要回复"的路径统一走 `_rate_limited()` 检查。

### 6.2 智能模式的设计原则

群聊插话的难点不在"能不能回"，而在**"什么时候不该回"**。一个在群里逢话必接的机器人，比一个安静的机器人更让人讨厌。因此采用**五层漏斗**，从最便宜到最贵逐层过滤，绝大多数消息在第 0-2 层就被拦掉，只有极少数进入 LLM 判定。

```
群消息流入
    │
    ▼
┌─────────────────────────────────────────┐
│ L0 硬规则（<1ms，命中即回复）            │  必回
│   • @机器人 / 引用机器人消息              │
│   • 唤醒词："小助手"、"bot"               │
│   • 指令前缀："/ask"、"#help"             │
└────────────────┬────────────────────────┘
                 │ 未命中
                 ▼
┌─────────────────────────────────────────┐
│ L1 黑名单/开关（<1ms，命中即丢弃）        │  必不回
│   • 群未开启机器人 / 群在静默名单          │
│   • 用户黑名单 / 消息来自机器人自身        │
│   • 消息类型非文本（图片/语音/文件）       │
│   • 静默时段（00:00-07:00）                │
└────────────────┬────────────────────────┘
                 │ 通过
                 ▼
┌─────────────────────────────────────────┐
│ L2 启发式打分（<1ms，得分低于阈值即丢弃）  │  打分
│   • 是否含疑问词（吗/呢/怎么/为什么/？）   │  +30
│   • 是否连续追问（最近3条内有机器人发言）  │  +25
│   • 消息长度适中（10-200 字）             │  +15
│   • 是否含求助词（求助/请问/有没有人知道） │  +20
│   • 纯表情 / 纯"哈哈哈" / 广告链接        │  -100
│   → 得分 < 40：直接丢弃（拦截约 80%）     │
└────────────────┬────────────────────────┘
                 │ 得分 ≥ 40（约 20% 进入下一层）
                 ▼
┌─────────────────────────────────────────┐
│ L3 小模型判定（~500ms，免费模型）         │  判定
│   输入：机器人人设 + 最近 8 条群消息 + 当前消息│
│   输出：YES / NO（max_tokens=16）         │
│   超时 3s 未返回 → 视为 NO（保守）        │
└────────────────┬────────────────────────┘
                 │ YES（约 5%）
                 ▼
┌─────────────────────────────────────────┐
│ L4 冷却与配额（<1ms，命中即丢弃）         │  限流
│   • 单群 60s 内已回过 → 丢弃              │
│   • 单群 5min 内已回 2 次 → 丢弃          │
│   • 单群 1h 内已回 8 次 → 丢弃            │
│   • 单账号当日回复 > 800 条 → 丢弃        │
│   • 概率闸门：70% 概率回复（消除必答感）  │
└────────────────┬────────────────────────┘
                 │ 通过（约 1-2% 的消息最终被回复）
                 ▼
              生成回复
```

### 6.3 L2 启发式打分实现

```python
QUESTION_WORDS = ["吗", "呢", "怎么", "如何", "为什么", "为啥", "哪个", "什么时候",
                  "能不能", "可以吗", "有没有", "是不是", "?", "？"]
HELP_WORDS = ["求助", "请问", "有人知道", "谁知道", "帮忙", "急", "在线等"]
NOISE_PATTERNS = [r"^[哈呵嘿嘻\s]+$", r"^\[.*\]$", r"^https?://\S+$", r"^\d+$"]

class HeuristicScorer:
    def score(self, msg: WxMessage, recent: list[WxMessage]) -> int:
        s, text = 0, msg.content.strip()

        if any(w in text for w in QUESTION_WORDS):       s += 30
        if any(w in text for w in HELP_WORDS):           s += 20
        if 10 <= len(text) <= 200:                       s += 15
        if any(re.match(p, text) for p in NOISE_PATTERNS): s -= 100

        # 最近 3 条内有机器人发言 → 说明正在对话中，追问概率高
        if any(m.sender_id == msg.bot_id for m in recent[-3:]):
            s += 25

        # 消息里出现机器人的名字（未用 @ 的软呼叫）
        if "小助手" in text or "bot" in text.lower():
            s += 40

        return s
```

### 6.4 L3 小模型判定提示词

用免费模型做二分类，是这套方案成本可控的关键。

```python
JUDGE_PROMPT = """你是一个群聊机器人的"插话决策器"。

【机器人人设】
{persona}

【群聊最近消息】
{recent_messages}

【当前消息】
{current_message}

【任务】
判断机器人现在是否应该插话回复。判断标准：
- 有人在向机器人提问、求助、或明显在等机器人回应 → YES
- 有人点名机器人（即使没 @）→ YES
- 群友之间闲聊、互相聊天、与机器人无关的话题 → NO
- 机器人插话会显得突兀、打断别人对话 → NO
- 机器人刚发过言且话题已结束 → NO

只输出 YES 或 NO，不要任何其他内容。"""
```

**成本核算**：假设群日均消息 2000 条，L2 后进入 L3 的约 400 条，每条判定消耗约 300 token 输入 + 5 token 输出。用 GLM-4.7-Flash（免费）→ **判定成本 ¥0**。若用付费小模型（如 qwen-flash ¥0.3/M），日均成本约 ¥0.04。

### 6.5 群聊配置模型

每个群独立配置，支持差异化策略：

```sql
CREATE TABLE group_config (
    chat_id        VARCHAR(64) PRIMARY KEY,
    group_name     VARCHAR(128),
    enabled        BOOLEAN     DEFAULT TRUE,   -- 总开关
    mode           VARCHAR(16) DEFAULT 'smart',-- smart | mention_only | keyword_only
    wake_words     JSONB       DEFAULT '["小助手"]',
    persona        TEXT,                       -- 该群的专属人设
    cooldown_sec   INT         DEFAULT 60,     -- 冷却
    hourly_limit   INT         DEFAULT 8,      -- 每小时上限
    reply_prob     REAL        DEFAULT 0.7,    -- 回复概率
    quiet_hours    JSONB       DEFAULT '[0,1,2,3,4,5,6]',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
```

三种模式：
- **smart**：完整五层漏斗（默认）
- **mention_only**：只响应 @ 和唤醒词，绝不主动插话（适合正式工作群）
- **keyword_only**：只响应关键词（适合客服群）

---

## 7. 会话状态持久化方案

### 7.1 三层存储架构

| 层 | 存储 | 内容 | TTL | 理由 |
|---|---|---|---|---|
| **热** | Redis | 上下文窗口、冷却计数、令牌桶、分布式锁、去重表 | 24h | 高频读写，需毫秒级 |
| **温** | PostgreSQL | 消息归档、会话元数据、群配置、用户画像、成本统计 | 永久 | 需事务、需查询 |
| **冷** | pgvector | 长期记忆向量、知识库 RAG | 永久 | 语义检索 |

**选型说明**：向量库用 **pgvector** 而非独立 Milvus——中小规模下，复用 PostgreSQL 可显著降低运维复杂度；当向量量级超过千万再考虑拆分。

### 7.2 Redis Key 设计

```
# 会话上下文（热）
ctx:private:{chat_id}              LIST    # 最近 N 轮问答对，JSON 序列化
ctx:group:{chat_id}                LIST    # 群聊共享上下文（含发言人标注）

# 长期摘要
summary:{chat_type}:{chat_id}      STRING  # 滚动摘要文本

# 触发控制
cooldown:group:{chat_id}           STRING  # 上次回复时间戳（TTL = cooldown_sec）
quota:group:{chat_id}:{yyyymmddhh} STRING  # 小时计数器（TTL = 3600）
quota:account:{bot_id}:{yyyymmdd}  STRING  # 当日总回复数

# 并发控制
lock:chat:{chat_id}                STRING  # 分布式锁（TTL = 120s）

# 幂等
dedup:{msg_id}                     STRING  # SET NX EX 3600

# 限流
bucket:global:{bot_id}             HASH    # 全局令牌桶
bucket:chat:{chat_id}              HASH    # 会话令牌桶

# 队列
stream:incoming                    STREAM  # 入站消息流（消费者组）
stream:outgoing                    STREAM  # 出站消息流
```

### 7.3 PostgreSQL 表结构

```sql
-- 消息归档（全量，用于审计与分析）
CREATE TABLE message_log (
    id          BIGSERIAL PRIMARY KEY,
    msg_id      VARCHAR(128) UNIQUE NOT NULL,
    platform    VARCHAR(32),
    chat_type   VARCHAR(16),
    chat_id     VARCHAR(64),
    sender_id   VARCHAR(64),
    sender_name VARCHAR(128),
    content     TEXT,
    msg_type    VARCHAR(16),
    is_at_me    BOOLEAN,
    direction   VARCHAR(8),      -- in | out
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_msg_chat_time ON message_log(chat_id, created_at DESC);

-- 会话元数据
CREATE TABLE conversation (
    chat_id        VARCHAR(64) PRIMARY KEY,
    chat_type      VARCHAR(16),
    first_seen_at  TIMESTAMPTZ DEFAULT NOW(),
    last_active_at TIMESTAMPTZ DEFAULT NOW(),
    turn_count     INT DEFAULT 0,
    total_tokens   INT DEFAULT 0,
    summary        TEXT           -- 长期摘要持久化副本
);

-- 用户画像（仅私聊，群聊不建画像）
CREATE TABLE user_profile (
    user_id      VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128),
    tags         JSONB DEFAULT '[]',
    preferences  JSONB DEFAULT '{}',
    last_seen_at TIMESTAMPTZ DEFAULT NOW()
);

-- 长期记忆（向量）
CREATE TABLE memory_vector (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   VARCHAR(64),      -- user_id（私聊）
    content    TEXT,
    embedding  vector(1024),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_mem_vec ON memory_vector USING hnsw (embedding vector_cosine_ops);

-- 成本统计
CREATE TABLE llm_usage (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     VARCHAR(64),
    model       VARCHAR(64),
    prompt_tok  INT,
    completion_tok INT,
    cost_yuan   NUMERIC(10,6),
    cached_hit  BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

### 7.4 会话生命周期

```
新消息到达
   │
   ├─ Redis 有 ctx:{chat_id}？
   │     否 → 检查 DB conversation 表是否有历史
   │           有 → 用 summary 恢复"冷启动上下文"
   │           无 → 全新会话
   │     是 → 直接使用
   │
   ├─ 追加消息到 ctx（LTRIM 保持长度上限）
   │
   ├─ token 超预算？ → 触发滚动摘要 → 更新 summary + DB
   │
   └─ 每 5 分钟或会话结束时，异步刷盘到 message_log / conversation
```

**TTL 策略**：Redis 上下文 TTL = 30 分钟。30 分钟无交互则视为会话结束，下次消息触发"冷启动"（仅带摘要，不带完整历史）。这样既省内存，又符合真人对话的节奏感。

**进程重启恢复**：Orchestrator 无状态，全部状态在 Redis/DB。重启后消费者组从 `last_delivered_id` 继续消费，会话上下文从 Redis 自动恢复。**唯一需注意的是进行中的 LLM 请求**——需在锁上设置合理 TTL，并在启动时清理僵尸锁。

---

## 8. 部署架构建议

### 8.1 开发 / 小规模（单机 Docker Compose）

```yaml
# deploy/docker-compose.yml
services:
  gateway:
    image: gewechat/gateway:latest        # iPad 协议网关
    ports: ["2531:2531", "2532:2532"]
    volumes: ["./data/gewechat:/root/.gewechat"]
    restart: always

  orchestrator:
    build: ..
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATABASE_URL=postgresql://bot:bot@postgres:5432/wxbot
      - LLM_PRIMARY_BASE=https://api.deepseek.com
      - LLM_PRIMARY_KEY=${DEEPSEEK_KEY}
      - LLM_JUDGE_BASE=https://open.bigmodel.cn/api/paas/v4
      - LLM_JUDGE_KEY=${GLM_KEY}
    depends_on: [redis, postgres]
    restart: always

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes: ["./data/redis:/data"]

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: bot
      POSTGRES_PASSWORD: bot
      POSTGRES_DB: wxbot
    volumes: ["./data/pg:/var/lib/postgresql/data"]

  # 可选：可视化管理
  prometheus: { image: prom/prometheus:latest }
  grafana:    { image: grafana/grafana:latest }
```

**最低配置**：2C4G 云服务器即可跑通（不含 LLM 算力，LLM 走 API）。**建议出口 IP 固定**（绑定弹性公网 IP），避免异地登录风控。

### 8.2 生产架构

```
                    ┌──────────────┐
   iPad协议网关 ────→│  Nginx/SLB   │
   (独立容器/多实例)  └──────┬───────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
      ┌───────────────┐          ┌───────────────┐
      │ Orchestrator  │  ...     │ Orchestrator  │   ← 无状态，水平扩展
      │  Worker #1    │          │  Worker #N    │
      └───────┬───────┘          └───────┬───────┘
              │                          │
              └────────────┬─────────────┘
                           ▼
        ┌──────────────────────────────────────┐
        │  Redis Cluster  │  PG 主从  │ 监控    │
        └──────────────────────────────────────┘
```

**关键设计点**：

| 问题 | 方案 |
|---|---|
| **会话粘性** | 消费者组按 `hash(chat_id) % N` 分区，保证同一会话固定路由到同一 Worker（否则上下文竞争） |
| **网关单点** | iPad 协议网关是单点。方案：① 多账号横向扩展（每号一个网关）② 进程守护 + 自动重登（登录态失效告警） |
| **LLM 限流** | 网关层统一令牌桶，按 provider 配额限流；超额请求排队而非丢弃 |
| **灰度发布** | 按群维度灰度：先切 10% 的群到新版本 Worker |
| **密钥管理** | LLM API Key、网关 Token 走环境变量 / K8s Secret / 密钥管理服务，**严禁硬编码进代码或镜像** |

### 8.3 可观测性

**必须监控的指标**：

```
# 业务指标
bot_messages_received_total{chat_type, platform}
bot_replies_sent_total{chat_type, trigger_layer}    # 按触发层级统计，验证漏斗效果
bot_trigger_rejected_total{layer, reason}           # 各层拦截量
bot_reply_latency_seconds{stage}                    # 分段：判定/LLM首token/总耗时

# LLM 指标
llm_tokens_total{model, type}                       # type: prompt|completion|cached
llm_cost_yuan_total{model}
llm_errors_total{model, code}
llm_fallback_total                                  # 降级次数

# 系统指标
gateway_health{adapter}                             # 登录态
queue_depth{stream}                                 # 队列积压
redis_lock_wait_seconds
```

**告警规则**：
- `gateway_health == 0` 持续 60s → 登录态失效，立即告警（这是最致命的故障）
- `queue_depth > 1000` → 消息积压，触发降级
- `llm_fallback_total` 5 分钟内 > 10 → 主模型异常
- `bot_replies_sent_total` 单群 5 分钟内 > 5 → 疑似风控前兆，人工介入
- `ilink_errcode_total{code="-14"}` > 0 → **官方 bot_token 失效，需重新扫码登录**
- `ilink_getupdates_failures_total` 连续 > 5 → 官方通道网络异常，检查降级开关

**官方通道专属监控**（建议加到定时任务）：
```bash
# 官方是否开放群聊 —— 一旦 chatTypes 出现 "group"，即可迁移群聊到合规通道
npm view @tencent-weixin/openclaw-weixin version   # 版本变更
# 并检查包内 src/channel.ts 的 capabilities.chatTypes
```


**日志**：结构化 JSON，含 `msg_id` / `chat_id` / `trace_id`，便于全链路追踪。Loki 或 ELK 收集。

### 8.4 安全与合规清单

- [ ] 机器人账号使用**专用号**，非个人主号（私聊走官方 API 可放宽，群聊必须专用号）
- [ ] 官方 iLink 的 `bot_token` 按凭证管理：文件权限 0600，禁止入库、禁止打日志
- [ ] 出口 IP 固定，避免频繁变更
- [ ] LLM API Key 走密钥管理，不进代码仓库
- [ ] 用户消息脱敏存储（手机号、身份证号等正则脱敏后入库）
- [ ] 内容安全：敏感词前置过滤 + 模型侧兜底，违规内容不落库
- [ ] 提供"退出机制"：用户/群主可随时关闭机器人
- [ ] 群聊场景**明确告知**这是 AI 机器人（避免误导群成员）
- [ ] 遵守《生成式人工智能服务管理暂行办法》：使用已备案的国内大模型 API

---

## 9. 实施路线图

### Phase 0 · PoC 验证（1 周）

**目标**：先跑通**官方 iLink Bot API**（合规、零风险），再评估群聊方案的封号风险。

**Step 1 · 官方私聊链路（零风险，优先做）**

```
技术栈：@tencent-weixin/openclaw-weixin + OpenClaw
做法：
  1. 安装 OpenClaw（Node >= 22.13.0）+ 微信插件
  2. openclaw channels login 扫码登录（用测试号）
  3. 接入 DeepSeek API，跑通一对一对话
  4. 验证 sendtyping"正在输入"状态的体验效果
验证点：消息延迟、长轮询稳定性、token 是否长期有效
```

或使用本仓库的裸调适配器：`python -m src.verify_ilink` 先验证接口连通性。

**Step 2 · 群聊方案验证（需承担风险，用独立小号）**

```
技术栈：Gewechat / WeChatPadPro + 本仓库 Orchestrator
做法：
  1. Docker 部署协议网关，扫码登录**专用小号**（勿用主号）
  2. 拉一个测试群，验证 @ 回复与触发判定
  3. 观察 2-4 周，记录账号是否异常
验证点：稳定性、封号风险实测、五层漏斗的实际拦截率
```

**Step 3 · 决策点**：根据 Step 2 的风险实测结果，决定群聊是否上线，以及是否需要准备 A1（企微智能机器人）作为合规替代。

### Phase 1 · 核心中台自研（3-4 周）

**目标**：用自研 Orchestrator 替换 LangBot，实现本方案的核心能力。

```
交付物：
  ✅ Adapter 抽象层 + Gewechat/WeChatPad 适配器
  ✅ Redis Stream 消息总线 + 幂等 + 分区有序
  ✅ TriggerRouter 五层漏斗
  ✅ ContextManager 四层上下文 + 滚动摘要
  ✅ LLMGateway 多模型路由 + 降级 + 缓存
  ✅ RateLimitedSender 限流 + 拟人化
  ✅ Redis + PostgreSQL 持久化
```

### Phase 2 · 生产加固（2-3 周）

```
交付物：
  ✅ 多 Worker 水平扩展 + 会话粘性路由
  ✅ Prometheus 指标 + Grafana 看板 + 告警
  ✅ 群维度灰度发布
  ✅ 内容安全过滤
  ✅ 成本统计与配额
  ✅ 管理后台（群配置、人设管理、开关）
```

### Phase 3 · 能力增强（持续）

```
  ◻ RAG 知识库（pgvector + 文档检索）
  ◻ Function Calling（查天气/订单/日程）
  ◻ 图片理解（多模态模型）
  ◻ 语音消息（ASR 转文字后处理）
  ◻ 群聊话题总结 / 每日简报
  ◻ A/B 测试不同人设与触发阈值
```

---

## 10. 风险与合规提示

| 风险 | 等级 | 说明 | 缓解措施 |
|---|---|---|---|
| **群聊账号封禁** | 🔴 高 | 群聊走 iPad 协议，违反平台协议，微信风控持续收紧。2025-04 封号风暴中 Hook 类用户"几乎无一幸免" | 用专用号；严格限流；拟人化节奏；固定 IP；避免营销行为。**私聊已走官方 API，无此风险** |
| **官方 API 被限速/终止** | 🟠 中 | 官方条款 4.7、7.2 明确腾讯可决定接入的 AI 服务类型与频率，可拦截内容、可终止服务 | 做好双通道并行；私聊保留 B1 作为降级路径；不把核心业务完全依赖单一通道 |
| **协议失效** | 🟠 中 | 微信版本更新可能导致 iPad 协议失效，需持续适配 | 优先选维护活跃的协议项目；做好降级预案 |
| **合规风险** | 🟡 低 | 官方通道已合规；群聊场景未经告知的 AI 机器人发言可能引发争议 | 群聊中明确标识 AI 身份；提供退出机制；不采集群成员隐私 |
| **内容风险** | 🟠 中 | LLM 幻觉、敏感内容输出；官方条款 4.7 保留内容拦截权 | 敏感词过滤 + 模型侧安全策略 + 人工抽检 |
| **成本失控** | 🟡 低 | 群聊量大导致 LLM 调用费飙升 | 五层漏斗 + 免费模型做判定 + 缓存 + 配额上限 |
| **数据安全** | 🟠 中 | 聊天记录含敏感信息；官方会收集 IP/操作记录/设备信息 | 脱敏入库；加密存储；访问审计；向用户告知 |


> **强烈建议**：如果业务方对合规性有硬要求（如金融、医疗、政务），**请放弃个人微信协议方案**，改用企业微信官方智能机器人 + 微信客服的组合。虽然功能受限，但这是唯一无法律与账号风险的路径。

---

## 附录 A：技术选型决策树

```
需要"个人微信"好友私聊 / 外部微信群聊？
├── 仅私聊 ────────────────────────→ 【A5 官方 iLink Bot API】✅ 合规零封号
│
├── 仅群聊 ─┬─ 能接受封禁风险 + 有专用号？
│           │   ├── 是 ─┬─ 需要云端 7×24 + 高并发？
│           │   │       │   ├── 是 → 【B1 iPad 协议】
│           │   │       │   └── 否 → B1 或 B4（预算充足选 B4）
│           │   │       └── 仅本地测试验证 → 【B2 WCF】
│           │   └── 否 → ❌ 无合规可行方案，请调整需求
│           └──
│
├── 私聊 + 群聊 ──────────────────→ 【混合架构】★ 本方案推荐
│                                   私聊 = A5（合规） + 群聊 = B1（承担风险）
│
└── 否（仅需企业内部场景）─────────→ 【A1 企微智能机器人】✅ 官方支持群聊
                                      + 【A3 微信客服】做对外一对一
```


## 附录 B：关键参数速查

| 参数 | 建议值 | 说明 |
|---|---|---|
| 全局发送频率 | ≤ 20 条/分钟 | 风控安全线 |
| 单群发送频率 | ≤ 2 条/分钟 | **最关键的红线** |
| 单群冷却 | 60 秒 | 两次回复最小间隔 |
| 单群小时配额 | 8 条 | 防止刷屏 |
| 回复概率 | 0.7 | 消除"必答"机器感 |
| L2 启发式阈值 | 40 分 | 低于则丢弃 |
| L3 判定超时 | 3 秒 | 超时视为不回复 |
| 上下文窗口 | 5000 token | L2 短期记忆预算 |
| 摘要触发阈值 | 5000 token | 超过则滚动压缩 |
| 上下文 TTL | 30 分钟 | 无交互则会话结束 |
| 去重 TTL | 1 小时 | msg_id 幂等窗口 |
| 分布式锁 TTL | 120 秒 | 防止死锁 |
| LLM 首 token 超时 | 8 秒 | 超时提示用户 |
| LLM 整体超时 | 60 秒 | 超时降级重试 |

---

*文档结束。配套参考实现见 `src/` 目录，部署配置见 `deploy/` 目录。*
