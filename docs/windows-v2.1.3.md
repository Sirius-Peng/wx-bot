# Windows v2.1.3 发布与专项测试

## Windows 专项改进

- 原生回归定位并修复两项 Windows 文件锁问题：统计读取句柄允许文件删除/替换，清空记录不再遇到 WinError 32；首次迁移并发启动先获得字节锁再初始化锁文件。
- HTTP 客户端复用严格校验的 SSLContext，按证书路径和版本失效缓存，保留系统代理/自定义CA语义；不跨事件循环共享客户端或套接字。
- Python sidecar 改为原生 x64 **onedir**，安装包仍为 NSIS Setup / 单文件 Portable。安装版不再在每次后端重启时解压 Python、Jieba 和时区资源；便携包自身仍需解包。
- 新旧后端布局兼容：优先 `resources/backend/wxbot-server/wxbot-server.exe`，保留旧 `resources/backend/wxbot-server.exe` 回退。
- 同时点击启动/重启复用同一 readiness 任务；停止取消尚未完成的启动、退避重启与手动重启。旧进程的延迟退出事件不再误清理新进程。
- 启动超时先清理本次 sidecar；Windows 兜底以 PID 精确清理进程树，不按进程名结束其他应用。
- 原生退出走同一异步清理流程。启动错误页「重试」真正调用后端重启，不再仅重新加载错误页；错误内容使用 HTML 转义。
- `npm run dev` 使用 Node 跨平台设置环境变量，不再依赖 Windows 不支持的 POSIX `VAR=... command` 语法。
- 包验证比较完整 onedir 文件树及内容，并检查中文分词词典和 `Asia/Shanghai` 时区文件，不只校验 exe。

## 本版本累计功能

微信可靠投递/正在输入、图片识别、连续消息聚合与自适应等候、长期记忆与明确的跨平台身份关联、弗洛洛人格、QQ快速登录与连接状态修复、首启配置引导、独立本地数据和备份、联网搜索/当前时间、高频词/话题气泡统计一并进入 v2.1.3。

## 构建与测试

原生 Windows x64，Python 3.10+ 与 Node.js：

```powershell
cd desktop
npm ci
npm run test:windows
npm run pack:win
```

产物：`WxBot-2.1.3-Windows-x64-Setup.exe` / `WxBot-2.1.3-Windows-x64-Portable.exe`。
安装后无需 Python/Node。默认数据位于 `%LOCALAPPDATA%\WxBot\user-data`，不放安装目录；卸载不删除此目录。

离线回归不使用真实聊天或付费模型。原生安装测试在临时中文/空格目录进行；保留默认数据哈希，测试原生 x64 后端、认证、统计词典、时区、首启引导、备份/记忆、端口占用回退、重启、退出和卸载/重装。结果以发布时的验证报告为准，账号登录/模型密钥需要用户自行配置。

依据：[PyInstaller运行模式](https://pyinstaller.org/en/stable/operating-mode.html)说明 onefile 每次启动解包；[Electron before-quit](https://www.electronjs.org/docs/latest/api/app#event-before-quit)允许在退出前等待清理。
