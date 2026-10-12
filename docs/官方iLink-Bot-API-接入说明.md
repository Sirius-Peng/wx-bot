# 官方 iLink Bot API 接入说明

> 微信个人号官方 Bot 接口（微信 ClawBot / OpenClaw 微信渠道）
> 核验日期：2026-10-12 ｜ 核验对象：`@tencent-weixin/openclaw-weixin@2.4.10`

---

## 一、结论先行

| 能力 | 支持 | 说明 |
|---|---|---|
| **一对一私聊** | ✅ **完全支持** | 文本 / 图片 / 语音 / 文件 / 视频 |
| **群聊** | ❌ **不支持** | 源码能力声明为 `chatTypes: ["direct"]` |
| 合规性 | ✅ 官方产品 | 有《微信ClawBot功能使用条款》法律文件 |
| 封号风险 | ✅ 无 | 官方协议，非逆向 |
| "正在输入"状态 | ✅ 支持 | `sendtyping`，协议方案做不到 |
| 语音转文字 | ✅ 支持 | `voice_item.text` 直接给出结果 |
| 主动推送 | ⚠️ 受限于 context_token | 回复式交互为主 |

**一句话**：私聊场景**应当直接用它**，没有理由再冒协议方案的风险；群聊场景**用不了**，仍需 iPad 协议。

---

## 二、这是什么，为什么可信

腾讯在 2026-03 通过 [OpenClaw](https://docs.openclaw.ai)（一个 AI Gateway 框架）开放了个人微信的 Bot 能力。官方名称「微信 ClawBot 插件功能」，底层协议叫 **iLink**。

**这不是逆向，是官方产品**。以下是可独立复核的证据：

### 2.1 npm 包

| 项 | 值 |
|---|---|
| 包名 | `@tencent-weixin/openclaw-weixin` |
| 当前版本 | **2.4.10**（2026-10-10 发布） |
| author | **Tencent** |
| license | MIT |
| 维护者 | `fredynai@tencent.com`、`pumpkinxing@tencent.com`、`jfengjiang@tencent.com`、`yubingluo@tencent.com`、`unixliang@tencent.com`、`ruiqihong@tencent.com` |
| 版本数 / 首发 | 26 个版本，首版 2026-03-21 |
| 月下载量 | 约 9.97 万 |

```bash
# 自行复核
npm view @tencent-weixin/openclaw-weixin
curl -s https://registry.npmjs.org/@tencent-weixin/openclaw-weixin | jq '.versions | keys'
```

### 2.2 域名与接口

```bash
# 域名归属：CNAME 指向腾讯自有基础设施
dig +short ilinkai.weixin.qq.com
# → aewebpodproxy.weixin.qq.com

# 接口实测：返回真实二维码
curl "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3"
# → {"qrcode":"...","qrcode_img_content":"https://liteapp.weixin.qq.com/q/...","ret":0}
```

### 2.3 官方仓库

`github.com/Tencent/openclaw-weixin` — 935 star，171 fork，2026-10-11 仍在提交，未归档。

---

## 三、群聊为什么用不了（已核验）

这是最关键的一条边界，**已通过四重证据交叉确认**：

| 证据 | 内容 |
|---|---|
| 能力声明 | `src/channel.ts:230` → `capabilities: { chatTypes: ["direct"], ... }` |
| 入站硬编码 | `src/messaging/inbound.ts:151,266` → `ChatType: "direct"` |
| 变更日志 | 26 个版本的 CHANGELOG **零次提及群聊** |
| 源码实现 | 全量搜索 `群聊` / `群消息` / `chatroom` → **零命中** |

**注意**：`src/api/types.ts` 里确实定义了 `group_id?: string` 字段，但这只是**协议层的类型预留**，插件从未使用它。不要被这个字段误导——**有类型定义 ≠ 有功能实现**。

> 如果将来腾讯开放群聊，插件必然会先在 `capabilities.chatTypes` 里加入 `"group"`。**建议把这个字段作为监控点**，一旦变化即可考虑迁移。

---

## 四、协议详解

### 4.1 基础信息

- **Base URL**：`https://ilinkai.weixin.qq.com`
- **CDN**：`https://novac2c.cdn.weixin.qq.com/c2c`
- **协议**：HTTP/JSON，无需 SDK

### 4.2 请求头（固定套路）

```javascript
{
  "Content-Type": "application/json",
  "AuthorizationType": "ilink_bot_token",
  "X-WECHAT-UIN": base64(String(randomUint32())),  // 每次随机，防重放
  "Authorization": `Bearer ${bot_token}`            // 登录后才有
}
```

`X-WECHAT-UIN` 的构造：随机 uint32 → 转十进制字符串 → base64。**每次请求都要重新生成**。

### 4.3 端点列表

| Endpoint | Method | 功能 |
|---|---|---|
| `/ilink/bot/get_bot_qrcode` | GET | 获取登录二维码（`?bot_type=3`） |
| `/ilink/bot/get_qrcode_status` | GET | 轮询扫码状态（`?qrcode=xxx`） |
| `/ilink/bot/getupdates` | POST | **长轮询收消息**（核心） |
| `/ilink/bot/sendmessage` | POST | 发送消息 |
| `/ilink/bot/sendtyping` | POST | 发送"正在输入"状态 |
| `/ilink/bot/getconfig` | POST | 获取 typing_ticket |
| `/ilink/bot/getuploadurl` | POST | 获取 CDN 预签名上传地址 |
| `/ilink/bot/msg/notifystart` | POST | 上线上报 |
| `/ilink/bot/msg/notifystop` | POST | 下线上报 |

### 4.4 登录流程

```
开发者                    iLink 服务器              微信用户
   │                          │                       │
   │─ GET get_bot_qrcode ────▶│                       │
   │◀─── { qrcode, url } ─────│                       │
   │                          │◀──── 用户扫码 ────────│
   │─ GET get_qrcode_status ─▶│（轮询）                │
   │◀── { status:"confirmed", │                       │
   │      bot_token, baseurl }│                       │
   │                          │                       │
   │  持久化 bot_token，后续 Bearer 鉴权                │
```

`status` 的取值：
- `confirmed` — 登录成功，拿到 `bot_token`
- `binded_redirect` — 该账号已绑定过本端，**本地凭证仍有效**，不要当成失败
- `expired` / `cancelled` — 二维码失效

### 4.5 收消息：长轮询

```javascript
POST /ilink/bot/getupdates
{
  "get_updates_buf": "<上次返回的游标，首次为空字符串>",
  "base_info": { "channel_version": "1.0.2", "bot_agent": "MyBot/1.2.0" }
}
```

服务端 **hold 住连接最多 35 秒**，直到有新消息才返回。

```json
{
  "ret": 0,
  "msgs": [ /* WeixinMessage[] */ ],
  "get_updates_buf": "<新游标>",
  "longpolling_timeout_ms": 35000
}
```

⚠️ **`get_updates_buf` 必须每轮更新并持久化**，否则会重复收到消息。它等价于数据库的 cursor。

### 4.6 消息结构

```json
{
  "seq": 12,
  "message_id": "7300000000000001",
  "from_user_id": "o9cq800kum_xxx@im.wechat",
  "to_user_id": "e06c1ceea05e@im.bot",
  "session_id": "sess_1",
  "message_type": 1,
  "message_state": 2,
  "context_token": "AARzJWAFAAABAAAAAAAp...",
  "create_time_ms": 1770000000000,
  "item_list": [
    { "type": 1, "text_item": { "text": "你好" } }
  ]
}
```

**ID 规律**：用户 `xxx@im.wechat`，机器人 `xxx@im.bot`。

**枚举值**：

| 字段 | 取值 |
|---|---|
| `message_type` | 1 = 用户发出，2 = Bot 发出 |
| `message_state` | 0 = NEW，1 = GENERATING，2 = FINISH |
| `item_list[].type` | 1 文本，2 图片，3 语音，4 文件，5 视频，11 工具调用开始，12 工具调用结果 |

**语音消息**的 `voice_item.text` 直接带转文字结果，省掉一层 ASR。

### 4.7 context_token：最容易踩的坑

**每条入站消息都带 `context_token`，回复时必须原样带回**，否则消息关联不到正确的会话窗口。

```javascript
POST /ilink/bot/sendmessage
{
  "msg": {
    "to_user_id": "o9cq800kum_xxx@im.wechat",
    "message_type": 2,        // BOT 发出
    "message_state": 2,       // FINISH
    "context_token": "<从入站消息里取>",   // ← 必填！
    "item_list": [
      { "type": 1, "text_item": { "text": "你好！" } }
    ]
  }
}
```

**工程做法**：按 `chat_id` 缓存最近一条入站消息的 `context_token`，发送时取出。

### 4.8 "正在输入"状态

```javascript
// ① 先取 typing_ticket
POST /ilink/bot/getconfig
{ "base_info": { "channel_version": "1.0.2" } }
// → { "ret": 0, "typing_ticket": "<base64>" }

// ② 发送输入状态
POST /ilink/bot/sendtyping
{
  "ilink_user_id": "o9cq800kum_xxx@im.wechat",
  "typing_ticket": "<上一步拿到的>",
  "status": 1        // 1 = 正在输入，2 = 取消
}
```

这是**官方协议独有的体验优势**——协议方案无法在微信里显示"对方正在输入"。对 LLM 这种有思考延迟的场景，价值很大。

### 4.9 媒体文件

微信 CDN 上的媒体文件经过 **AES-128-ECB** 加密。发送图片流程：

1. 生成随机 AES-128 key
2. 用 AES-128-ECB 加密文件
3. `getuploadurl` 获取预签名上传地址（需提供明文大小、MD5、密文大小）
4. PUT 加密文件到 CDN
5. `sendmessage` 中带上 `aes_key`（base64）和 CDN 引用参数

---

## 五、快速开始

### 5.1 官方推荐方式（基于 OpenClaw）

```bash
# 1. 安装 OpenClaw（需 Node.js >= 22.13.0）
openclaw --version

# 2. 安装微信插件
npx -y @tencent-weixin/openclaw-weixin-cli install
# 或
openclaw plugins install "@tencent-weixin/openclaw-weixin"

# 3. 启用
openclaw config set plugins.entries.openclaw-weixin.enabled true

# 4. 扫码登录
openclaw channels login --channel openclaw-weixin

# 5. 重启网关
openclaw gateway restart
openclaw channels status
```

**多账号**：重复执行 `channels login` 即可。建议开启会话隔离：

```bash
openclaw config set session.dmScope per-account-channel-peer
```

### 5.2 裸调方式（不依赖 OpenClaw）

本仓库已实现：`src/adapters/ilink.py`

```python
from src.adapters.ilink import ILinkAdapter

adapter = ILinkAdapter(state_dir="./data/ilink", bot_agent="MyBot/1.0.0")

# 首次：扫码登录
await adapter.login()

# 注册消息处理器
adapter.on_message(my_handler)

# 启动（内部会跑长轮询循环）
await adapter.start()
await adapter.poll_loop()
```

验证脚本：

```bash
python -m src.verify_ilink
```

输出（实测）：

```
[A] 离线自检
  [OK] 请求头格式正确，UIN 示例 = 2026023945
  [OK] 未登录时不带 Authorization：True
  [OK] 文本消息归一化：chat_id=o9cq800kum_xxx@im.wechat
  [OK] context_token 已保留（回复时必填）
  [OK] 语音消息取到转文字结果
  [OK] BOT 自身消息被忽略（防回环）
  [OK] 能力声明：supports_private=True, supports_group=False
[B] 在线联调（真实调用腾讯官方接口）
  [OK] 真实调用成功 ret=0
  [OK] 二维码链接 = https://liteapp.weixin.qq.com/q/...
```

---

## 六、接入 LLM：完整链路

```
用户发消息
   ↓
getupdates 长轮询收到 WeixinMessage
   ↓
归一化为统一 WxMessage（保留 context_token）
   ↓
幂等去重（message_id）→ 上下文装配 → LLM 调用
   ↓
sendtyping(开启)  ← 让用户看到"正在输入"
   ↓
sendmessage（必须带 context_token）
   ↓
sendtyping(关闭)
```

**注意**：官方插件自身设置了 `blockStreaming: true`（合并参数 `minChars: 200`、`idleMs: 3000`），即**不做流式**，等回答完整后一次性发送。如果自研，可以用 `sendtyping` 缓解等待焦虑，效果比协议方案的分段发送更自然。

---

## 七、工程化注意事项

| 问题 | 做法 |
|---|---|
| **长轮询超时** | 属正常现象（服务端 35s 无消息即返回），应立即重连，不要当错误处理 |
| **游标丢失** | `get_updates_buf` 必须持久化，否则重启后重复收消息 |
| **token 失效** | `errcode = -14` 表示 token 失效，必须立即告警并重新扫码 |
| **防回环** | 过滤 `message_type == 2`（Bot 自己发出的消息），否则会自己回自己 |
| **并发** | 同一 `chat_id` 串行处理，避免上下文竞争 |
| **凭证安全** | `bot_token` 等同账号凭证，文件权限收紧到 `0600`，禁止入库/入日志 |
| **启停上报** | 启动调 `notifystart`、停止调 `notifystop`，便于服务端对账 |
| **内容安全** | 官方保留内容拦截权，不要依赖它做业务级内容风控，自己也要过滤 |

---

## 八、合规约束（来自官方条款）

腾讯随该功能发布了《微信ClawBot功能使用条款》，开发前必须了解：

| 条款 | 内容 | 工程含义 |
|---|---|---|
| 3.2 | 腾讯仅提供信息收发，**不存储输入内容与输出结果，不提供 AI 服务** | 腾讯只是管道，AI 输出由你负责 |
| 4.6 | 禁止**绕过、破解微信技术保护措施** | 不要拿这个 API 去做协议逆向的事 |
| 4.7 | 腾讯**有权决定**你可连接的第三方 AI 服务类型、范围、收发规模或频率，可**识别、拦截、阻断**内容 | 不要假设速率无上限；要有降级预案 |
| 5.3 | 收集 **IP 地址、操作记录、设备信息**用于安全审计 | 需向用户告知 |
| 7.2 | 腾讯**有权随时变更、中断、中止或终止**本功能 | ⚠️ **不要把核心业务完全依赖这套 API** |

> **架构建议**：私聊主用官方 iLink，但保留 iPad 协议作为降级通道。两者在适配器层等价（都归一化到 `WxMessage`），切换成本极低。

---

## 九、与本方案的关系

本方案采用**混合架构**：

| 会话类型 | 适配器 | 合规性 | 文件 |
|---|---|---|---|
| 私聊 | `ILinkAdapter`（官方） | ✅ 零风险 | `src/adapters/ilink.py` |
| 群聊 | `GewechatAdapter`（iPad 协议） | ⚠️ 有封禁风险 | `src/adapters/gewechat.py` |
| 路由 | `HybridAdapterRouter` | — | `src/adapters/hybrid.py` |

路由逻辑：

```python
class ILinkAdapter(BaseAdapter):
    supports_private = True
    supports_group   = False   # 官方 capabilities.chatTypes = ["direct"]

class GewechatAdapter(BaseAdapter):
    supports_private = True
    supports_group   = True    # iPad 协议支持群聊
```

上层（触发判定 / 上下文 / LLM / 发送器）**完全复用，零改动**。

---

## 十、监控建议

**必须监控**（按优先级）：

1. `capabilities.chatTypes` 是否新增 `"group"` —— **这是官方开放群聊的信号**，值得订阅 npm 版本变更
2. `errcode == -14` 出现次数 —— token 失效
3. `getupdates` 连续失败次数 —— 网络或服务异常
4. 单日消息收发量 —— 对比官方限速阈值
5. 插件版本 —— 腾讯更新频繁（26 个版本 / 7 个月），需跟进

```bash
# 订阅版本变更，最简单的方式是定时比对
npm view @tencent-weixin/openclaw-weixin version
```

---

*本文档基于 `@tencent-weixin/openclaw-weixin@2.4.10` 源码核验与真实接口联调，核验日期 2026-10-12。API 设计可能随版本迭代变化，请以官方仓库为准。*
