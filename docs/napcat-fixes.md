# NapCat 登录、消息链路与设置体验修复

> v3.0.3 开发说明：以下内置网关流程对应当前源码；已发布 v3.0.2 安装包不会因文档修改自动获得该功能。版本与实际平台以[发布索引](README.md)为准。

## 用户操作
- 连接 → QQ：默认使用应用内置的 Windows x64 原生 NapCat；由后端准备与启动，用户无需另装 QQ、Node.js、Docker 或 WSL2。配置与账号留在既有活动目录的 `napcat/`。保存过的账号可刷新后选择「快速登录」；勾选「下次自动登录」才写入自动登录账号。首次、手机验证或失效时保留扫码。
- 运行方式可切换为外部 OneBot；只连接已配置地址，不管理外部进程。本次 macOS 没有内置网关，不回退安装 Docker，应使用外部 OneBot。旧容器与登录卷原样保留，不自动搬运Linux登录凭证。
- 快速登录的接口成功仅代表提交；持续查询 CheckLoginStatus 的 isLogin，真实在线后才完成。二维码生成并解码后才弹窗。
- v2.1.5模型与行为的九个配置卡片（主/备用/判定/视觉模型、搜索、人格、回复、记忆、主动联系；现有Windows v2.1.4仍为八个、不含主动联系）分别保存，展示未保存/已保存。保存人格不会提交模型草稿；切页、状态刷新保留草稿。通道也单独保存。PATCH 只传本区变化字段，以最新配置合并，避免自动部署新生成的 Token 被旧表单清空。
- 消息推送状态区分反向 WS、最近 HTTP 上报、机器人是否已启动和 QQ 是否在线。HTTP 上报显示时间，不冒充长连接。

## 采用的 NapCat 功能
1. GetQuickLoginListNew + GetQuickLoginList 兼容列表；SetQuickLogin 使用 uin；SetQuickLoginQQ 由用户明确选用。
2. OB11Config GetConfig/SetConfig 热更新本应用命名的 http-server 与 wxbot-reverse-ws，不改第三方网关项/插件，不退出 QQ。旧版离线兜底读取并解析本应用 JSON 配置，校验回读，真正变更才重启。
3. OneBot get_login_info/get_status/get_version_info 并行检查，离线不宣称登录。QQ 私聊正在输入扩展继续保留。
4. WebUI 凭证失效只重新鉴权一次。部署重复点击合并成一个任务。原生管理器后台准备与启动，前端状态请求共享进行中的查询；不再调用 Docker 安装/检测来阻塞网关启动。

## Windows 修复
- subprocess stdout/stderr 可以为 None 或 bytes：先归一化 UTF-8，再 strip；日志同时保留两路内容。
- 连接注册独立于 QQ adapter：启动前、重启前后和多连接断开均准确显示。
- 原生网关和管理页面使用本机回环连接，按真实后端端口回填反向 WS，不使用 Docker 的 `host.docker.internal`。QQ 登录、OneBot接口可达、反向WS/HTTP推送与机器人运行分别显示；不把某一项在线当作全部消息链路已就绪。
- 旧版Windows Docker callback-only服务属于历史兼容路径，不作为新原生网关前置条件。外部OneBot仍需正确配置实际地址与Token。
- 新原生凭证在本地配置与 `napcat/`；旧持久化卷不删改。回归使用临时配置，不发送真实用户消息。

## 下载源与历史镜像设置
- Windows x64 内置 NapCat 与其运行环境随应用提供，不在点击启动时要求拉取容器镜像。具体上游固定版本、摘要和再分发声明随本次打包清单记录，不以“有镜像缓存”冒充原生已安装。
- 旧 `napcat_image_mirror` 字段仅保留配置兼容，不显示为原生下载源、不在保存新通道设置时重写；不修改用户机器的全局 npm/pip/Docker 配置。
- Python 启动/打包/容器依赖：清华 PyPI 优先，官方源兜底；PIP_INDEX_URL 可覆盖。
- 桌面 .npmrc 与 lockfile 使用 npmmirror，Electron 与 builder 二进制也指定大陆镜像；原有包 integrity 校验不变。
- Dockerfile/旧容器路径仍可保留其DaoCloud设置供历史部署，不能把它们写成新桌面版的QQ启动前提。
- 已验证 DaoCloud NapCat/Alpine manifest、npmmirror registry/目录可访问；未把此连通性当成下载速度基准。

## 回归
新增 test_napcat_fixes.py：空日志、编码、国内源/回退、缓存、容器复用、快速登录协议、热更新保留第三方配置、凭证刷新、真实 HTTP/WS 回调入口、分区保存、密钥掩码、登录前连接注册、离线健康检查及回滚均单独验证。UI 使用隔离样例网关，不代表真实 QQ 已登录。

## 一手参考
- [NapCat QQLogin 源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-webui-backend/src/api/QQLogin.ts)
- [NapCat OB11Config 源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-webui-backend/src/api/OB11Config.ts)
- [NapCat Docker 持久化配置](https://github.com/NapNeko/NapCat-Docker)
- [DaoCloud 大陆镜像](https://github.com/DaoCloud/public-image-mirror)
- [清华 PyPI 使用说明](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)

## 当前设置卡片

新增微信角色语音后，当前共有十个独立设置卡片（不含通道），语音单独保存，不与模型或人格草稿混存。v2.1.5 的九卡历史说明保持不变。

## 停止与恢复

详细连接页提供「停止内置网关」，仅内置且本应用拥有的运行进程/启动阶段可操作。外部模式隐藏且不发送stop请求；停止不删除profile，关闭自动恢复，重新启动再启用。重复点击只发一次请求，在途启动、二维码解码、账号查询、快速登录与登录确认都以代次校验，停止后不会重开晚到的弹窗。首页仍只保留启动按钮。

## 原生前端专项

`tests/test_native_napcat_frontend.cjs` 验证显式模式、不触发Docker准备、Windows原生启动、macOS外部网关提示、仅保存模式增量、旧镜像字段保留、外部进程不接管及首页单按钮。原有引导专项继续覆盖真实二维码解码后弹窗、取消晚到结果、同一启动任务与快速登录。本机VM/DOM桩不是实际QQ账号登录或Windows原生进程验收，原生发行证据另列。
