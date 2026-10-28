# WxBot 桌面版 · v3.0.3

v3.0.3 正在构建与验收，新增Windows x64后端托管的内置NapCat；继续保留v3.0.2微信角色语音、历史日期、首页/设置与桌面伙伴修复。v3.0.1 仅为此前本地构建，未单独公开发布；历史发布附件保持原版本。

当前发布[v3.0.2](https://github.com/Sirius-Peng/wx-bot/releases/tag/v3.0.2)。本次平台、真实摘要、资源及未执行项目见[发布清单](../docs/release-v3.0.2.json)。已弃用本地判定模型，运行与安装包不使用ONNX。文档更新：2026-10-28。新版见[发布准备记录](../docs/release-v3.0.3.json)。历史[v3.0.0清单](../docs/release-v3.0.0.json)与附件保留原版本。

v3复用微信/QQ内核增加独立桌面文字会话、默认静态形象和本地Live2D导入；桌面伙伴仍仅文字；v3.0.2 可选的微信角色语音使用独立 Fish Audio Key、页内试听与后台 WAV 文件附件，见[语音说明](../docs/v3/wechat-voice.md)；它不等同于桌面语音或微信原生语音条。管理界面收敛为首页、接入设置、人格与记忆、设置；原数据路径、凭证、应用ID保持不变。详见[v3总览](../docs/v3/README.md)、[接话策略与历史研究](../docs/v3/local-decision.md)及[验收状态](../docs/v3/validation.md)。

## v3.0.3 原生 QQ 网关（开发中）

Windows x64 源码使用应用内置的 NapCat 和运行环境，无需另外安装 QQ、Node.js 或 Docker；首页一键启动、快速登录与真实二维码弹窗保留。macOS 当前只提供外部 OneBot 连接，不自动准备 Docker。配置和账号新增于既有活动目录的 `napcat/`；旧容器/持久卷不删改，跨系统登录可能需首次重新确认。详见[原生网关](../docs/v3/native-napcat.md)。以下v3.0.2附件仍保持原发布内容。

## v3桌面开发入口

1. 使用原有Python虚拟环境安装根目录`requirements.txt`，在`desktop`执行`npm ci`和`npm start`。
2. 在首页启用独立桌面会话，点击「打开桌面伙伴」；默认账号不与微信/QQ自动关联，主模型沿用已有配置。
3. 无模型时显示默认静态标记，可直接文字聊天。导入Live2D时选择包含`.model3.json`及其依赖资源的目录，多清单时选择要加载的模型。导入位于用户数据目录`companion/models/`，不是安装目录。
4. 伙伴窗口可置顶与鼠标穿透；点击「关闭」、按 Esc 或 Ctrl/Cmd+W 可关闭伙伴，机器人仍在后台运行。托盘「关闭桌面伙伴」在穿透模式下也可使用，「显示桌面伙伴／恢复鼠标」可重新打开。增量是草稿，最终完成才持久保存，重连恢复不会重新生成同一轮。

源码已加入官方Cubism Core/Framework渲染资源，用户启动不下载SDK。重建渲染资源时先单独获取官方SDK 5-r.5，并保留其原始许可证，再执行：

```sh
WXBOT_CUBISM_SDK="/absolute/path/CubismSdkForWeb-5-r.5" node desktop/scripts/build-live2d.cjs
```

接话采用保护规则和轻量云端判定，不加载、预热、下载或复制本地判定模型。运行依赖不再要求ONNX Runtime、tokenizers或numpy；PyInstaller显式排除这些模块及训练框架，避免开发环境中残留的依赖进入新包。模型研究目录和旧权重保持原位，不因应用更新被删除。

```sh
node desktop/scripts/verify-runtime-resources.cjs --source-only
node tests/test_cloud_package.cjs
```

浏览器、原生开发窗口、冻结应用及安装生命周期属于不同验证层。本次平台与完成范围见[发布清单](../docs/release-v3.0.2.json)，开发历史见[v3验收记录](../docs/v3/validation.md)。未执行的Windows交互IME、真实账号登录或NSIS完整安装/卸载不记为通过；云端速度和费用以实际API usage为准，不沿用历史ONNX成绩。

主回复保留fast/deep和已有模型/Key；判定使用独立配置或原有复用规则。上下文采用稳定会话前缀，窗口自动/手填、默认40%后压缩；官方DeepSeek 1M与未知模型32K自动值及真实缓存统计见[会话与KV缓存](../docs/v3/context-session.md)。

## 当前发布产物：v3.0.2

| 平台 | 文件 | 状态 |
|---|---|---|
| Windows x64安装版 | `WxBot-3.0.2-Windows-x64-Setup.exe` | 原生构建；具体通过/未执行范围见本次清单 |
| Windows x64便携版 | `WxBot-3.0.2-Windows-x64-Portable.exe` | 原生构建；具体通过/未执行范围见本次清单 |
| macOS Apple Silicon | `WxBot-3.0.2-macOS-arm64.dmg` | 原生构建；具体通过/未执行范围见本次清单 |
| 校验文件 | `SHA256SUMS.txt` | 对应本次实际发布平台 |

下载见[项目首页](../README.md#下载与平台)，精确摘要及未执行项目见[本次清单](../docs/release-v3.0.2.json)。未列出的架构没有本次桌面包；以下v3.0.0/v2.1.7表保留历史，不是当前下载。

常用 Windows 用户建议选 Setup；Portable 首次启动包含自解压，本次单机样本约40.41秒，Unpacked约4.03秒、冻结后端约2.9—3.3秒。这些是启动观察，不是 LLM 响应速度或所有机器的时延保证。

## 历史已发布产物：v3.0.0

| 平台 | 文件 | 状态 |
|---|---|---|
| Windows x64安装版 | `WxBot-3.0.0-Windows-x64-Setup.exe` | 保留历史发布及其验收 |
| Windows x64便携版 | `WxBot-3.0.0-Windows-x64-Portable.exe` | 保留历史发布及其验收 |
| macOS Apple Silicon | `WxBot-3.0.0-macOS-arm64.dmg` | 保留历史发布及其验收 |

历史下载见[v3.0.0 Release](https://github.com/Sirius-Peng/wx-bot/releases/tag/v3.0.0)，摘要与未执行项目见[原清单](../docs/release-v3.0.0.json)，不以本次文档覆盖原验收。

## 历史已发布产物：v2.1.7

| 平台 | 文件 | 当前状态 |
|---|---|---|
| Windows x64安装版 | `WxBot-2.1.7-Windows-x64-Setup.exe` | 原生x64构建，NSIS引导程序与包内资源核验；未重复完整安装/卸载循环 |
| Windows x64便携版 | `WxBot-2.1.7-Windows-x64-Portable.exe` | 原生x64构建，冻结后端、管理器重启/退出及实际便携进程/API启动核验 |
| macOS arm64 | `WxBot-2.1.7-macOS-arm64.dmg` | arm64、冻结API、源码与只读DMG内容核验 |
| 校验文件 | `SHA256SUMS.txt` | 对应本次三个安装包 |

v2.1.7未发布其他架构；构建和验证细节见[清单](../docs/release-v2.1.7.json)。历史Release及校验文件不修改。

## 架构和资源布局

```text
Electron: 窗口 / 托盘 / IPC / Backend进程管理
                  │ 127.0.0.1:17800（占用时空闲端口）
                  ▼
Python: 统一对话内核 / iLink / OneBot / 桌面 / LLM / 人格 / 记忆
                  │
                  ▼
固定用户数据目录（不是安装目录）
```

| 平台 | Python 打包方式 | 内置可执行文件 |
|---|---|---|
| macOS | PyInstaller onefile | `resources/backend/wxbot-server` |
| Windows | PyInstaller onedir | `resources/backend/wxbot-server/wxbot-server.exe`，同时包含 `_internal/` |

Windows 优先新 onedir 路径，仍兼容旧平铺 `resources/backend/wxbot-server.exe`。不要只复制 onedir 内的 exe；词典、时区和 Python 支持文件同样需要。网页同时作为独立 `resources/web` 和后端冻结资源提供。v3不再配置`decision-model`额外资源，也不将历史研究目录打入安装包。

图标只复制至外部 `resources/assets`，由 `paths.assetsDir()` 统一定位；ASAR不再重复保存同一套图标。仓库中的各平台图标仍保留供开发与原生构建使用。

主要模块：`src/main/companion.js` 伙伴窗口；`src/main/index.js` 编排；`window.js` 窗口；`tray.js` 托盘；`backend.js` 启动/健康/重启/日志；`lifecycle.js` 退出守卫与错误页；`ipc.js` 白名单；`paths.js` 后端与数据路径；`src/preload/index.js` 提供 `wxbotDesktop` 桥接。渲染页面不启用 Node 集成。

## 开发

先在仓库根目录建立 Python 3.10+ 虚拟环境并安装 `requirements.txt`，再安装 Node.js 和桌面锁定依赖：

```sh
cd desktop
npm ci
npm start
# 或跨平台开发入口
npm run dev
```

开发态优先仓库 `.venv`，Windows 查找 `.venv/Scripts/python.exe`，macOS/Linux 查找 `.venv/bin/python3`，并检查解释器版本。图标已随仓库提供；需要重新生成时执行 `npm run icons`，使用带 Pillow 的 Python。

## 原生构建

以下为原生构建入口；本次真正发布平台以清单为准。不再要求本地模型质量门；仍须通过无模型资源检查与平台原生测试。`pack:*`先检查源码策略，再重建后端；直接调用electron-builder时也有`beforePack`检查实际资源，拒绝旧ONNX/tokenizer/训练模块。原生构建不能用旧sidecar或空目录替代。

### macOS

```sh
cd desktop
npm ci
npm run pack:mac
# 需要 Intel 时，在原生 Intel 环境准备匹配的 Python/Electron架构
npm run pack:mac:x64
```

`pack:mac` 先构建 onefile 后端，再生成 DMG，最后校验 Electron 主程序、Framework 和 Python 的架构。后端脚本另有真实 `/healthz` 冒烟。构建日志出错或仅出现 DMG 文件不等于验收通过，需要检查完整退出状态、架构与 `hdiutil verify`。

### Windows x64

```powershell
cd desktop
npm ci
npm run test:windows
npm run pack:win
```

`build-backend.bat` 调用 PowerShell 脚本，要求原生 x64 Python 3.10+，构建 onedir 并检查 PE；`pack:win` 随后生成 Setup/Portable 并验证 PE、ASAR 版本、完整后端文件树、Jieba 词典和 `Asia/Shanghai` 时区资源。v2.1.7已验证中文/空格路径；v3须重新验证，不继承历史结论。

需要逐段排查时：`npm run build:backend:win`、`npm run verify:win`；macOS/Linux 后端入口为 `npm run build:backend`。`pack:all` 只选择当前原生平台，不在 macOS 交叉构建 Windows Python 引擎。输出均在 `desktop/dist`。

### 包装后的离线验证

在 Windows 的仓库根目录，用原生 Node/Python运行：

```powershell
node tools/verify_windows_manager.cjs "BACKEND.exe" "MANAGER_REPORT.json"
python tools/verify_windows_backend.py --binary "BACKEND.exe" --report "BACKEND_REPORT.json"
```

这里的 `BACKEND.exe` 指完整 onedir 中的 sidecar，而不是 Setup 安装器；报告放到已有的可写目录。临时数据不会使用真实账号/模型 Key。v2.1.7历史发布验收记录见[测试矩阵](../docs/validation.md)，完整文件摘要见[发布清单](../docs/release-v2.1.7.json)。

## 启动、托盘和退出

- 后端首选固定端口17800，避免反向WS配置随每次启动变化；占用时回退，并由运行时使用真实端口。
- 主窗口关闭只是隐藏；伙伴窗口关闭会释放伙伴窗口，后台与平台连接均继续。托盘「打开主界面」恢复；「退出」、应用退出入口及 `Cmd/Ctrl+Q` 走统一清理。
- 并发启动/重启复用同一个就绪任务。停止会取消待启动/退避重启；旧子进程退出事件不覆盖新实例。启动超时先清理本次进程。
- 优先调用认证的本机 shutdown；Windows 超时仅按本次 PID 清理进程树，不按名称结束其他进程。
- 错误页「重试启动后端」真正重启服务，不是重新加载错误页。详细日志在数据目录的 `logs/backend.log`。
- macOS 隐藏后可收起 Dock；托盘失败时保留应用入口。

## 数据位置

| 平台 | 默认活动数据 |
|---|---|
| Windows | `%LOCALAPPDATA%\WxBot\user-data` |
| macOS | `~/Library/Application Support/WxBot/user-data` |
| Linux源码 | `$XDG_DATA_HOME/wxbot/user-data`，默认 `~/.local/share/wxbot/user-data` |

Electron 自身 profile/cache 与应用长期数据不是同一个目录。普通卸载保留长期数据，`deleteAppDataOnUninstall: false` 同时保留旧 profile；新版本复制迁移可识别的旧数据，不覆盖现有目标。自定义目录优先，实际路径从界面查看；自行放回安装目录时需先备份，不能沿用默认独立目录的保留前提。v3.0.3内置NapCat的配置与完整QQ账号保存在原数据目录的 `napcat/`；外部网关和历史Docker卷不接管、不删改。恢复与备份见[持久化说明](../docs/user-data-persistence.md)。

## 故障排查

| 现象 | 检查 |
|---|---|
| 后台启动失败 | 错误页重试、帮助菜单重启；检查数据目录日志、完整 sidecar 资源和匹配架构 |
| QQ在线但收不到消息 | 区分QQ登录、机器人启动和反向WS/HTTP上报；原生网关使用本机回环和OneBot Token；外部网关核对其实际推送地址 |
| 设置没有生效 | 使用对应卡片保存按钮；通道变化依接口提示重启机器人，其他支持的配置热更新 |
| 记忆似乎丢失 | 查看实际数据路径/自定义环境变量、迁移报告与本地备份，不要先覆盖旧目录 |
| 首次打开系统拦截 | 包未配置代码签名，按系统对本次下载的验证/打开流程处理；不要把文件存在当成校验成功 |
| 安装退出后仍有后台 | 检查本版本的退出守卫/认证shutdown与日志；关闭窗口≠退出 |

本项目未签名发布，不承诺某个系统提示或绕过步骤在所有系统版本上相同。当前v3.0.2的运行时验证和文件摘要见[发布清单](../docs/release-v3.0.2.json)；历史v3.0.0、v2.1.7与v2.1.4记录保持原版本；文档更新不会修改已发布安装包或移动发布标签。
