# 微信输入状态与发送故障核查（2026-10-13）

## 附件里的事实
- 19:24:35之前有微信 sendmessage 成功请求；19:24:39开始新一轮处理。
- 第425行，19:25:18：模型 SDK 在 TLS 建连链路报 httpx2.ConnectError，最终为 openai.APIConnectionError。
- 第467行，19:25:45：sendmessage 建连超时，转为 DeliveryError；收件/完整草稿保留重试。
- 第649、917行，19:26:20与19:26:52：后续投递仍发生 ConnectTimeout。
- 第1183行，19:26:54：微信发送重新返回 HTTP200；第1186行模型请求也恢复HTTP200，随后继续发送。
- getupdates 在上述区间持续HTTP200，因此并非单纯收件功能故障。日志出现8份ConnectTimeout堆栈，但包含同一次故障在发送器、编排层和适配器的重复打印，不能算作8次独立故障。
- 全部1234行中没有 sendtyping 请求记录。只能证实输入状态未被记录，无法单凭日志断定是旧版本、配置关闭、缺票据还是辅助请求超时。
- 日志中文含非UTF8字节，记录过程有Windows编码不一致。没有根据乱码杜撰错误文本。

证据指向模型与微信发送的出站连接/TLS异常；代理、当地网络或服务端链路的具体责任点，这份日志未能区分。也不是当时API Key必然错误：故障前后已有模型HTTP200。接口HTTP200/ret0并不证明微信客户端已经显示或用户已阅读。

## 源码里的薄弱环节与修复
1. 输入状态原外层1秒限时包含getconfig+sendtyping两次请求，较慢票据获取容易被提前取消。ON预算改为3秒，与主回复并行；OFF预算0.8秒，不再刷新票据。只有决定回复并开始处理后才开启，不在等待用户继续输入时显示。
2. 票据按聊天用户缓存，启动获取到的owner票据直接复用。明确发送status1，完成/取消时发status2。ON响应丢失或取消后也尝试OFF；坏票据下次刷新。诊断公开 starts/stops/failed/last_status/error_type，不公开票据。
3. 微信getupdates使用独立HTTP连接池，避免长轮询与sendmessage/typing复用同一池。发送显式connect3秒、pool2秒、read15秒，保留长轮询40秒read。
4. 发送网络错误最多立即重试一次，用独立短生命周期连接，沿用完整草稿相同client_id/context_token/text；不关闭其他聊天在用的连接池。两次都失败仍保存到磁盘，继续原有退避。业务错误/凭证错误不当网络错误无限重试。
5. 模型使用SDK原生Timeout（当前SDK内部是httpx2，不能混用外部httpx的Timeout类型），connect8秒/pool3秒。未输出token时最多用临时客户端重试一次；输出过token后不拼接另一轮回答。
6. 微信模型网络故障保留原始收件，等恢复后重新回答，不把“你再说一遍”的错误占位文本存成待投递答案。已经生成的正文遇到发送故障仍只重发正文，不重新调用模型。
7. 首页区分“模型网络异常，保留消息待重试”与“发送网络异常，等待重试”，并提供接口输入状态计数。Windows冻结入口强制stdout/stderr UTF8，不更改TLS验证、代理或API Key。

## 本地验证
临时收件与假票据、模拟HTTP/SDK错误；覆盖慢于1秒的票据、1/2状态、取消后关闭、辅助请求不拖慢回复、同ID发送重试、草稿保留、模型断线后原问题恢复、首token前重试和部分流不拼接。未使用真实微信凭证、用户聊天、付费Key；用户附加日志原文件只读，哈希单独核对。

这些代码需进入重新构建的Windows软件后才能影响已安装版本。本次不是现有exe已被替换的证明。

## 一手参考
- [腾讯协议说明](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)：getconfig票据、sendtyping的1/2含义、HTTP与业务状态分别检查。
- [腾讯API实现](https://github.com/Tencent/openclaw-weixin/blob/main/src/api/api.ts)：票据按用户获取，context_token可附带。
