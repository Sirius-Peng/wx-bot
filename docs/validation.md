# v2.1.4 验证矩阵

发布日期：2026-10-14（Asia/Shanghai）；运行时代码提交 `78c384836d13eff63efe7ea0a7d33ba28e05552a`；[Release](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.4)、[文件清单](release-v2.1.4.json)。后续发布文档提交不改变该运行时代码。

| 层次 | 结果 | 验证范围 |
|---|---|---|
| macOS源码Python / Windows11 x64原生Python | 各533项，1项跳过 | 现有492项及旧目录导入、UTF-8控制文件；默认大小写不敏感文件系统跳过大小写冲突夹具 |
| 生命周期 / 存储路径 / 首启 / 统计 Node | 15 / 12 / 11 / 18项通过 | 延续原有回归，不以旧测试日志替代新源码运行 |
| 旧目录导入前端 | 10项通过 | 预览、确认、选择器、取消、晚到响应、重启重试 |
| 输入焦点 | 23项通过 | 去除原生confirm、独立确认决策、IME事件保护、过期close事件、焦点生命周期、目录选择finally |
| 最终Windows包 | 通过 | 原生x64 PE、ASAR/main/window/preload及全部Web资源匹配、onedir词典/时区与完整树 |
| 最终Portable原生启动与管理器 | 通过 | 实际Electron/冻结后端启动、认证、引导/统计/导入/确认资源、重启与退出 |
| macOS重建 | 通过 | 同一运行时代码重新冻结；Electron/Framework/Python arm64，ASAR/Web/后端匹配 |
| macOS DMG / 冻结导入 | 通过 | 压缩卷校验、只读挂载与App资源一致，旧人格/记忆/路径/未知文件/空目录/备份及重启恢复 |

最终焦点修复版 Setup 未重复完整安装/卸载/重装循环；该生命周期验证来自同日早先 v2.1.4 候选包。Windows交互式键盘/IME和真实微信/QQ账号扫码未实测。浏览器实际输入确认操作后仍正常，Node覆盖IME事件分支；这两项不等同于Windows实机输入质量验收。

复现：`python -m unittest discover -s tests`、`node tests/test_data_import_frontend.cjs`、`node tests/test_input_focus.cjs`、`python tools/check_docs.py`；Mac冻结导入为 `python tools/smoke_data_import.py --backend desktop/resources/backend/wxbot-server`。资源核验与原生Windows入口仍见[桌面说明](../desktop/README.md)。

---

# 历史：v2.1.3 验证矩阵

发布日期：2026-10-14（Asia/Shanghai）。运行时提交 `aba974b5c31f7fa9958830d226848eca5a7c225a`；[公开Release](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.3)。文档提交可以晚于该运行时提交。

| 层次 | 发布时结果 | 能证明什么 |
|---|---|---|
| macOS源码Python回归 | 492/492 | 临时文件、合成消息、模拟SDK/HTTP、认证、持久化、合并/取消、搜索/时间、统计、存储与平台合同 |
| Windows11 x64源码Python回归 | 492/492 | 上述逻辑在Python3.13.9原生Windows通过，含真实文件锁/路径语义 |
| Windows生命周期Node | 15项 | 合并启动/重启、取消、旧事件、超时清理、退出守卫、错误页重试 |
| 存储路径Node | 12项 | 固定平台路径、自定义路径、迁移参数、卸载配置 |
| 首启前端Node | 11项 | 任务单飞、增量保存、二维码实际响应/解码、取消晚到响应 |
| 统计前端Node | 18项 | 气泡面积/无重叠、键盘、文字注入不执行、筛选竞态、空/错误结果 |
| 原生Windows包核验 | 通过 | x64 PE、ASAR版本、完整onedir树及内容、Jieba词典、IANA时区 |
| Setup生命周期 | 通过 | 安装、原生桌面进程/HTTP后端启动、管理器重启/退出、卸载/重装和测试数据marker保留 |
| Portable真实启动 | 通过 | 便携外壳启动内置后端，内置页面、引导/统计资源与认证可用 |
| 冻结iLink/LLM夹具 | 通过 | 真实SDK到本地模拟接口，完整消息、业务拒绝、同草稿/ID/上下文重启恢复；记忆队列、非法JSON、重试、保存及注入 |
| QQ离线链路 | 通过 | 真实HTTP/反向WS到虚拟OneBot与假模型；鉴权、群@、静默、私聊、去重、记忆和停止 |
| macOS arm64包 | 通过 | Electron主程序/Framework/Python均arm64、DMG校验有效 |
| 公开发布 | 4/4摘要匹配 | main发布时与v2.1.3标签指向相同运行时提交；三个二进制及SHA256SUMS大小/摘要与附件匹配 |

## 测试环境与结果解释

Windows 11 Pro x64（10.0.26200）、Python3.13.9 x64、Node24.20.0；Electron36.9.5。Windows构建/运行数据使用中文和空格路径，Setup生命周期使用独立安装目录，不修改用户真实聊天。发布时有针对性地定位了两项Windows基线失败：统计读句柄阻止清空以及首次迁移锁初始化竞争，修复后原生回归全绿。

Setup和Portable是真实进程/API启动验证，不是远端截图或完整视觉UI验收。二维码/快速登录前端检查使用模拟状态；未用真实微信/QQ账号扫码、真实视觉模型质量评估或付费搜索调用替代离线回归。

冻结投递/记忆夹具明确关闭搜索、合并窗口和拟人等待，以隔离投递与积累；搜索、突发合并、自然等待分别由源码测试覆盖。不能据此声称所有功能组合或所有网关/模型版本已端到端实测。

同机3次冻结后端启动中位数：4259.89ms→3384.45ms；本机HTTP工厂暖创建332.08ms→0.09ms。系统缓存、网络和后台负载会影响结果，首个TLS客户端仍需初始化；这些数字不是模型生成或微信客户端送达时间。

## 复现入口

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_windows_lifecycle.cjs
node tests/test_storage_paths.cjs
node tests/test_onboarding_frontend.cjs
node tests/test_statistics_frontend.cjs
python tools/smoke_ilink.py
python -m src.verify_qq
python tools/qr_check.py
python tools/check_docs.py
```

使用项目虚拟环境；Windows原生构建入口是 `cd desktop && npm run pack:win`，其他环境不能代替其PE/安装验证。打包工具自带后端API及管理器检查，见[桌面说明](../desktop/README.md)。

完整发布文件尺寸和SHA256见[release-v2.1.3.json](release-v2.1.3.json)；下载校验可复现，真实账号/供应商行为需单独验证，不把结构测试通过当成人格效果、识图质量或平台客户端已读证明。
