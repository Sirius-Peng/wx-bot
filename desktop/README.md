# WxBot 桌面版 · v2.1.5

当前源码版本为v2.1.5，新增主动联系（单端无需关联、双端可选共享计划）、内容型联网总结和混合媒体改进；继续保留旧目录整套导入和Windows输入焦点修复。文档更新：2026-10-15。

本次先发布macOS arm64；Windows构建机离线，Windows最新安装包仍为v2.1.4，旧包不含v2.1.5新增功能。[下载v2.1.5](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.5) · [Windows v2.1.4](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.4) · [使用首页](../README.md) · [文档索引](../docs/README.md) · [测试矩阵](../docs/validation.md)

## 发布产物

| 平台 | 文件 | 当前状态 |
|---|---|---|
| macOS arm64 | `WxBot-2.1.5-macOS-arm64.dmg` | 本次发布；三项arm64、冻结API、源码字节码及只读挂载267项一致 |
| v2.1.5校验文件 | `SHA256SUMS.txt` | 只对应本次DMG |
| Windows x64 | `WxBot-2.1.4-Windows-x64-Setup.exe` / `WxBot-2.1.4-Windows-x64-Portable.exe` | 历史v2.1.4下载保留，不属于本次v2.1.5附件 |

本次没有Intel Mac、Windows或Linux AppImage/universal新包。原生构建入口不等于该平台已构建发布；Windows v2.1.4历史验证范围见对应[清单](../docs/release-v2.1.4.json)。

## 架构和资源布局

```text
Electron: 窗口 / 托盘 / IPC / Backend进程管理
                  │ 127.0.0.1:17800（占用时空闲端口）
                  ▼
Python: Web/API / iLink / OneBot / LLM / 人格 / 记忆 / 统计
                  │
                  ▼
固定用户数据目录（不是安装目录）
```

| 平台 | Python 打包方式 | 内置可执行文件 |
|---|---|---|
| macOS | PyInstaller onefile | `resources/backend/wxbot-server` |
| Windows | PyInstaller onedir | `resources/backend/wxbot-server/wxbot-server.exe`，同时包含 `_internal/` |

Windows 优先新 onedir 路径，仍兼容旧平铺 `resources/backend/wxbot-server.exe`。不要只复制 onedir 内的 exe；词典、时区和 Python 支持文件同样需要。网页同时作为独立 `resources/web` 和后端冻结资源提供。

图标只复制至外部 `resources/assets`，由 `paths.assetsDir()` 统一定位；ASAR不再重复保存同一套图标。仓库中的各平台图标仍保留供开发与原生构建使用。

主要模块：`src/main/index.js` 编排；`window.js` 窗口；`tray.js` 托盘；`backend.js` 启动/健康/重启/日志；`lifecycle.js` 退出守卫与错误页；`ipc.js` 白名单；`paths.js` 后端与数据路径；`src/preload/index.js` 提供 `wxbotDesktop` 桥接。渲染页面不启用 Node 集成。

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

`build-backend.bat` 调用 PowerShell 脚本，要求原生 x64 Python 3.10+，构建 onedir 并检查 PE；`pack:win` 随后生成 Setup/Portable 并验证 PE、ASAR 版本、完整后端文件树、Jieba 词典和 `Asia/Shanghai` 时区资源。路径包含中文/空格的原生源码与运行数据已验证。

需要逐段排查时：`npm run build:backend:win`、`npm run verify:win`；macOS/Linux 后端入口为 `npm run build:backend`。`pack:all` 只选择当前原生平台，不在 macOS 交叉构建 Windows Python 引擎。输出均在 `desktop/dist`。

### 包装后的离线验证

在 Windows 的仓库根目录，用原生 Node/Python运行：

```powershell
node tools/verify_windows_manager.cjs "BACKEND.exe" "MANAGER_REPORT.json"
python tools/verify_windows_backend.py --binary "BACKEND.exe" --report "BACKEND_REPORT.json"
```

这里的 `BACKEND.exe` 指完整 onedir 中的 sidecar，而不是 Setup 安装器；报告放到已有的可写目录。临时数据不会使用真实账号/模型 Key。当前发布验收记录见[测试矩阵](../docs/validation.md)，完整文件摘要见[发布清单](../docs/release-v2.1.5.json)。

## 启动、托盘和退出

- 后端首选固定端口17800，避免反向WS配置随每次启动变化；占用时回退，并由运行时使用真实端口。
- 窗口关闭只是隐藏，后台与平台连接继续。托盘「打开主界面」恢复；「退出」、应用退出入口及 `Cmd/Ctrl+Q` 走统一清理。
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

Electron 自身 profile/cache 与应用长期数据不是同一个目录。普通卸载保留长期数据，`deleteAppDataOnUninstall: false` 同时保留旧 profile；新版本复制迁移可识别的旧数据，不覆盖现有目标。自定义目录优先，实际路径从界面查看；自行放回安装目录时需先备份，不能沿用默认独立目录的保留前提。NapCat 登录状态属于独立 Docker 卷。恢复与备份见[持久化说明](../docs/user-data-persistence.md)。

## 故障排查

| 现象 | 检查 |
|---|---|
| 后台启动失败 | 错误页重试、帮助菜单重启；检查数据目录日志、完整 sidecar 资源和匹配架构 |
| QQ在线但收不到消息 | 区分QQ登录、机器人启动和反向WS/HTTP上报；Windows Docker使用独立callback-only端口及OneBot Token |
| 设置没有生效 | 使用对应卡片保存按钮；通道变化依接口提示重启机器人，其他支持的配置热更新 |
| 记忆似乎丢失 | 查看实际数据路径/自定义环境变量、迁移报告与本地备份，不要先覆盖旧目录 |
| 首次打开系统拦截 | 包未配置代码签名，按系统对本次下载的验证/打开流程处理；不要把文件存在当成校验成功 |
| 安装退出后仍有后台 | 检查本版本的退出守卫/认证shutdown与日志；关闭窗口≠退出 |

本项目未签名发布，不承诺某个系统提示或绕过步骤在所有系统版本上相同。当前v2.1.5与历史v2.1.4的运行时验证和文件摘要分别记录在对应发布清单；文档更新不会修改已发布安装包或移动发布标签。
