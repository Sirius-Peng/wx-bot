# 微信 AI 助手 · 桌面版

用 Electron 封装的桌面应用：**有关闭即后台静默运行、有系统托盘入口、可打包成单一安装包**。

---

## 架构：外壳与引擎分离

```
┌─────────────────────────────────────────────────────────────┐
│  Electron 主进程                                             │
│                                                              │
│   ┌────────────────┐  ┌────────────────┐  ┌──────────────┐  │
│   │ WindowManager  │  │  TrayManager   │  │   Backend    │  │
│   │ 窗口           │  │ 系统托盘       │  │ 子进程管理    │  │
│   │ 关闭=隐藏      │  │ 后台入口       │  │ 健康检查/重启 │  │
│   └────────────────┘  └────────────────┘  └───────┬──────┘  │
│                                                    │         │
│   ┌────────────────────────────────────────────────▼──────┐ │
│   │  preload（contextBridge）                             │ │
│   │  只暴露白名单方法，渲染进程拿不到 Node                │ │
│   └───────────────────────────────────────────────────────┘ │
└──────────────────────────────┬──────────────────────────────┘
                               │ http://127.0.0.1:<随机端口>
┌──────────────────────────────▼──────────────────────────────┐
│  渲染进程 —— 直接复用仓库里的 src/web 界面                    │
│  （与浏览器版是同一套 HTML/CSS/JS，通过 wxbotDesktop 增强）    │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│  Python 引擎（PyInstaller 打成单文件二进制）                  │
│  适配器 / 触发判定 / 上下文 / LLM 网关 / 发送器                │
└─────────────────────────────────────────────────────────────┘
```

**为什么不让 Electron 重写引擎？**
Python 引擎有 6400 行、39 项测试覆盖，且大量逻辑（五层触发漏斗、群聊 @ 检测的三种格式、U+2005 分隔符处理、上下文压缩）都是踩坑换来的。用 sidecar 方式复用，比用 Node 重写一遍风险低得多，而且**用户机器上不需要装 Python**（引擎已打包成二进制）。

---

## 目录结构

```
desktop/
├── package.json                Electron 工程配置
├── electron-builder.yml        打包配置（dmg / nsis / portable）
├── src/
│   ├── main/                   主进程（Node 环境，有完整系统权限）
│   │   ├── index.js            入口：编排所有模块 + 应用生命周期
│   │   ├── window.js           窗口管理（★ 关闭=隐藏）
│   │   ├── tray.js             系统托盘（★ 后台运行的前台入口）
│   │   ├── backend.js          Python 子进程管理（健康检查/重启/日志）
│   │   ├── ipc.js              IPC 白名单接口
│   │   └── paths.js            跨平台路径解析（开发态 vs 打包态）
│   └── preload/
│       └── index.js            contextBridge 安全桥接
├── backend/
│   └── entry.py                PyInstaller 打包入口
├── scripts/
│   ├── build-backend.sh        macOS/Linux：打包 Python 后端
│   ├── build-backend.bat       Windows：打包 Python 后端
│   └── make-icons.py           生成应用/托盘图标
├── assets/                     图标（icon.png/icns/ico、tray*.png）
└── resources/backend/          ← 打包产物（wxbot-server[.exe]）
```

**模块化设计**：每个模块职责单一、互不依赖。`window.js` / `tray.js` / `backend.js` 都不知道对方存在，全部由 `index.js` 编排。想换掉 Python 后端，只改 `backend.js` + `paths.js` 两个文件即可。

---

## 开发调试

```bash
cd desktop
npm install

# 首次需要生成图标
npm run icons

# 启动（会自动拉起仓库根目录 .venv 里的 Python 后端）
npm start
```

**开发态行为**：`paths.js` 检测到未打包时，会用仓库根目录的 `.venv/bin/python3 -m src.server` 作为后端。所以改 Python 代码后重启应用即可生效，不用重新打包。

---

## 打包发布

### 第一步：打包 Python 后端

```bash
npm run build:backend        # macOS / Linux
npm run build:backend:win    # Windows
```

脚本会：
1. 用 PyInstaller 把整个 Python 引擎打成单文件二进制
2. 把 `src/web` 静态资源一起塞进去
3. **自动做冒烟测试** —— 真的启动一次并探测 `/healthz`，不通过就直接失败

产物：`resources/backend/wxbot-server`（约 20-30 MB）

### 第二步：打包桌面应用

```bash
npm run pack:mac     # 产出 .dmg（arm64 + x64）
npm run pack:win     # 产出 NSIS 安装包 + 免安装 exe
npm run pack:all     # 两个平台都打
```

产物在 `desktop/dist/`：

| 平台 | 文件 | 说明 |
|---|---|---|
| macOS | `微信AI助手-1.0.0-arm64.dmg` | 拖进 Applications 即可 |
| macOS | `微信AI助手-1.0.0-x64.dmg` | Intel Mac |
| Windows | `微信AI助手-Setup-1.0.0.exe` | 安装向导，可自选目录 |
| Windows | `微信AI助手-1.0.0-portable.exe` | 免安装单文件 |

> **跨平台限制**：macOS 上只能打 mac 包，Windows 上只能打 win 包。要同时出两个平台的包，需要在对应系统上分别执行（或用 CI）。

### 未签名应用首次打开

本项目未配置代码签名证书，因此：

- **macOS**：首次打开会提示"无法验证开发者"。右键点应用 → **打开** → 再点"打开"即可（只需一次）。若提示已损坏，执行：
  ```bash
  xattr -cr "/Applications/微信AI助手.app"
  ```
- **Windows**：SmartScreen 会提示"未知发布者"，点"更多信息" → "仍要运行"。

要消除这些提示，需要购买 Apple Developer（$99/年）和 Windows 代码签名证书，然后在 `electron-builder.yml` 里配置。

---

## 后台运行是怎么实现的

这是桌面版最核心的行为，由三处配合完成：

**① 拦截窗口关闭事件**（`window.js`）
```javascript
this.win.on('close', (e) => {
  if (!app.isQuitting) {
    e.preventDefault();   // 不销毁窗口
    this.hide();          // 只隐藏
  }
});
```

**② 关掉所有窗口不退出**（`index.js`）
```javascript
app.on('window-all-closed', () => {
  // 故意留空 —— 应用继续在托盘里运行
});
```

**③ 托盘作为前台入口**（`tray.js`）

菜单结构刻意保持极简，用户关掉窗口后能凭直觉找到出路：

```
打开主界面          ← 恢复窗口
────────────────
服务状态：运行中     ← 一眼看到机器人是否在跑
重启后台服务
────────────────
退出                ← 真正退出（会先停掉后端）
```

**真正退出的三个入口**（都会先优雅停止 Python 进程）：
1. 托盘菜单 → 退出
2. 界面「模型与行为」页 → 桌面应用 → 退出应用
3. `Cmd/Ctrl+Q`

**macOS 细节**：窗口隐藏后 Dock 图标也会收起，更像"后台静默"；点 Dock 图标或托盘菜单可恢复。若托盘图标加载失败，则不会收起 Dock 图标，保证用户永远有入口。

---

## 数据存放位置

代码目录与数据目录分离 —— 卸载/重装应用不会丢数据：

| 平台 | 数据目录 |
|---|---|
| macOS | `~/Library/Application Support/wxbot-desktop/data/` |
| Windows | `%APPDATA%\wxbot-desktop\data\` |
| Linux | `~/.config/wxbot-desktop/data/` |

日志在 `.../wxbot-desktop/logs/backend.log`。界面里可以一键打开这两个目录。

> 打包配置里 `deleteAppDataOnUninstall: false` —— **卸载时不删数据**，避免误删配置和聊天记录。要彻底清理请手动删除上述目录。

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| 窗口显示"后台服务启动失败" | 点「查看运行日志」看 `backend.log`；常见原因是端口被占或后端二进制缺失 |
| 托盘图标不见了 | 菜单栏图标可能被系统折叠；macOS 可在「系统设置 → 控制中心」里调整 |
| 关闭窗口后找不到应用 | 看菜单栏托盘图标；macOS 也可点 Dock 图标 |
| 打包后启动白屏 | 检查 `extraResources` 里的 `web` 目录是否真的被打进去了 |
| 首次打开提示"已损坏" | `xattr -cr "/Applications/微信AI助手.app"` |
| 打包报 `asar` 写入失败 | 若你的环境有文件写入限制（企业沙箱等），可临时加 `--config.asar=false` 跳过归档。正式发布建议保持 asar 开启（体积更小、启动更快） |
| 打包报 `hdiutil detach` 失败 | DMG 其实已经生成，只是卸载虚拟磁盘失败。检查 `dist/*.dmg` 是否存在，存在即成功 |
| 启动报 `NODE_OPTIONS is not supported` | 无害提示，是外部环境设置了 `NODE_OPTIONS`，Electron 打包应用会忽略它 |

---

## 实测记录

以下是在 macOS 26.5.2 / arm64 上跑通的完整链路（可作为验收参考）：

```
① 后端打包
   $ npm run build:backend
   ✓ 后端打包完成：resources/backend/wxbot-server（22M）
   ✓ 冒烟测试通过，后端可正常启动

② 桌面应用打包
   $ npm run pack:mac
   ✓ dist/微信AI助手-1.0.0.dmg（128M）
   ✓ dist/mac-arm64/微信AI助手.app（270M）

③ 打包产物核查
   ✓ Contents/MacOS/微信AI助手
   ✓ Contents/Resources/backend/wxbot-server（22M，内置引擎）
   ✓ Contents/Resources/web/index.html
   ✓ Info.plist: 微信AI助手 / com.siriuspeng.wxbot / 1.0.0

④ 运行打包后的应用
   [spawn] .../微信AI助手.app/Contents/Resources/backend/wxbot-server (port=49413)
   [ready] http://127.0.0.1:49413
   → 打包版正确识别打包态，使用内置二进制而非系统 Python
```

---

## 已知限制

- **未签名**：首次打开需要用户手动放行（见上文）
- **不支持跨平台交叉打包**：在 mac 上打不出 win 包
- **sidecar 体积**：打包后约 120-150 MB（Electron 运行时占大头，Python 引擎约 25 MB）
- **仅 x64/arm64**：未提供 32 位版本
