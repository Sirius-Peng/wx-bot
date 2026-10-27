# NapCat 登录、消息链路与设置体验修复

> 当前状态：已随 [v2.1.3](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.3) 发布；文档同步于2026-10-14。使用与测试范围见[文档索引](README.md)及[验证矩阵](validation.md)。

## 用户操作
- 接入设置 → QQ：一键启动网关会复用既有容器，不重建 QQ 登录卷。保存过的账号可刷新后选择「快速登录」；勾选「下次自动登录」才写入 NapCat 自动登录账号。首次、手机验证或失效时保留扫码。
- 快速登录的接口成功仅代表提交；持续查询 CheckLoginStatus 的 isLogin，真实在线后才完成。二维码生成并解码后才弹窗。
- v2.1.5模型与行为的九个配置卡片（主/备用/判定/视觉模型、搜索、人格、回复、记忆、主动联系；现有Windows v2.1.4仍为八个、不含主动联系）分别保存，展示未保存/已保存。保存人格不会提交模型草稿；切页、状态刷新保留草稿。通道也单独保存。PATCH 只传本区变化字段，以最新配置合并，避免自动部署新生成的 Token 被旧表单清空。
- 消息推送状态区分反向 WS、最近 HTTP 上报、机器人是否已启动和 QQ 是否在线。HTTP 上报显示时间，不冒充长连接。

## 采用的 NapCat 功能
1. GetQuickLoginListNew + GetQuickLoginList 兼容列表；SetQuickLogin 使用 uin；SetQuickLoginQQ 由用户明确选用。
2. OB11Config GetConfig/SetConfig 热更新本应用命名的 http-server 与 wxbot-reverse-ws，不改第三方网关项/插件，不退出 QQ。旧版离线兜底读取并解析本应用 JSON 配置，校验回读，真正变更才重启。
3. OneBot get_login_info/get_status/get_version_info 并行检查，离线不宣称登录。QQ 私聊正在输入扩展继续保留。
4. WebUI 凭证失效只重新鉴权一次。部署重复点击合并成一个任务。Docker 检测放在线程，前端状态请求共享进行中的查询。

## Windows 修复
- subprocess stdout/stderr 可以为 None 或 bytes：先归一化 UTF-8，再 strip；日志同时保留两路内容。
- 连接注册独立于 QQ adapter：启动前、重启前后和多连接断开均准确显示。
- Windows 桌面后端管理页面仍仅监听 127.0.0.1。Docker 经 host.docker.internal 连接独立动态端口的 callback-only 服务；仅允许 /ws/onebot 与 /onebot/event，QQ 开关与非空 OneBot Token 双重校验。管理接口在该端口返回 404。实际 socket 端口回填网关，不假定 8000。
- 凭证只在本地配置/持久化卷；回归测试用临时配置，不发送真实用户消息。

## 大陆下载源
- NapCat、Alpine 与 Docker 构建 Python 基础镜像：DaoCloud docker.m.daocloud.io；NapCat/Alpine 拉取失败自动回退官方，并优先复用缓存。
- Python 启动/打包/容器依赖：清华 PyPI 优先，官方源兜底；PIP_INDEX_URL 可覆盖。
- 桌面 .npmrc 与 lockfile 使用 npmmirror，Electron 与 builder 二进制也指定大陆镜像；原有包 integrity 校验不变。
- 设置中的容器镜像源可调整或清空；不修改机器全局 npm/pip/Docker 配置。Docker Desktop 安装程序保留官方厂商下载地址。
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
