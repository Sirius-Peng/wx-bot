# 用户数据独立持久化

> 当前状态：已随 [v2.1.3](https://github.com/Sirius-Peng/wx-bot/releases/tag/v2.1.3) 发布；文档同步于2026-10-14。使用与测试范围见[文档索引](README.md)及[验证矩阵](validation.md)。

## 默认目录（桌面版与源码启动一致）

| 系统 | 位置 |
|---|---|
| macOS | `~/Library/Application Support/WxBot/user-data` |
| Windows | `%LOCALAPPDATA%\WxBot\user-data`（未设置 LocalAppData 时用用户目录下 `AppData\Local`） |
| Linux | `$XDG_DATA_HOME/wxbot/user-data`；未设置有效绝对路径时 `~/.local/share/wxbot/user-data` |

目录名固定为 WxBot / wxbot，不跟应用版本、安装路径、产品显示名、Electron profile 或工作目录改变。更新替换应用二进制、删除 `.app`、运行普通 NSIS 卸载后，该目录继续存在。NSIS 仍保持 `deleteAppDataOnUninstall: false`，旧 Electron profile 也不被主动清理。应用没有“卸载时删除记忆”的逻辑。

显式 `WXBOT_DATA_DIR` 优先级最高；历史 `WXBOT_USER_DATA_DIR` 继续表示隔离 Electron profile，数据保存在其 `data` 子目录。自定义路径不要放在安装目录/临时目录，卸载后继续保留所选目录。隔离路径默认不自动吸收其他账号的旧数据，可显式使用 `WXBOT_MIGRATE_LEGACY=1` 和 JSON 路径数组 `WXBOT_LEGACY_DATA_DIRS` 指定迁移来源。

## 哪些内容保留

- `config.json`：用户人格、关系模式、模型 Key、搜索/行为与通道设置。
- `memory.json` 及版本备份：v3渐进记忆、身份关联、事实证据/有效期、固定和明确遗忘屏蔽；不合并不同账号数据。
- `conversation-state.json`：会话轮次版本、待承接发言、静默/延后决策、期限及投递尝试；同样位于独立目录，重启不伪造新来话。
- `context-ilink.json` / `context-onebot.json`：独立平台近期上下文与摘要。
- `messages.jsonl`：消息记录。
- `proactive.json`（v2.1.5）：主动联系的联系人开关、计划、近期互动、共享等待状态及必要微信会话凭据，同样位于独立数据目录并随整套备份/导入保留。
- `ilink/`：微信账号、上下文票据、游标、待处理消息、回复草稿/发件队列等状态。
- `qq-bursts.json`、`onboarding.json`、`admin-token` 等本地运行/引导状态。
- 新的后端日志写在此目录下 `logs/`；旧日志不参与活动数据覆盖，但旧数据内日志保留在迁移 ZIP 中。

QQ 登录状态由 Docker 的 `wxbot-napcat-data` 和 `wxbot-napcat-config` 持久卷保管，不因更新/卸载本应用被删除。普通文件 ZIP 不包含 Docker 卷，请不要把 ZIP 当成 QQ 登录态的完整备份；手动删除 Docker 卷、Docker 数据或操作系统用户目录属于另外的数据删除操作。

## v3开发中的稳定会话与媒体资料

2026-10-23源码新增稳定会话前缀，仍在原活动数据目录与各人格空间中保存，不改已有context文件名、登录凭据或应用ID。内联视觉像素以本地私有媒体sidecar保存，主上下文JSON仅保留引用，恢复请求时还原原图；这与长期记忆事实、聊天文本日志及默认Live2D形象分别管理。

视觉会话迁移/备份需要主JSON与其配套媒体一起保留；只复制一个context文件不保证历史图片可恢复。资料依旧可能包含敏感聊天图片，应按与聊天记录相同的本地权限和备份保管方式处理；不会自动上传备份，但模型调用会发送当前使用的图像。明确遗忘撤销可用前缀与引用，不代表同时物理擦除原始文件、旧备份或供应商已接收的数据。

这部分仍属未发布v3开发行为，侧car权限、原生Windows读写性能、覆盖升级/导入与整套备份须按[v3验收](v3/validation.md)单独核对，历史v2.1.3的原生安装记录不替代它。完整预算与缓存边界见[会话与KV缓存](v3/context-session.md)。

## 自选旧目录导入（v2.1.4）

新版已有配置也可从「模型与行为 → 导入旧版数据」选择旧目录，预览并确认整套替换。此功能与下面的首次自动非覆盖迁移不同，需重启后台生效，详见[导入与恢复说明](data-import.md)。

## 首次迁移

在启动 ConfigStore、Runtime、管理员认证和记忆加载前执行迁移：

1. 桌面程序传递当前旧 profile 的 `data/` 和已知旧产品目录；源码优先使用当前项目的旧 `data/`。只识别具有本项目状态文件的目录。
2. 如果新目录已经有配置/记忆/上下文等有效状态，保留现有目录，不用旧数据覆盖或填补已被用户删除的内容。
3. 如果新目录为空，先为旧来源创建含逐文件 SHA256 清单的 ZIP，重新打开核对哈希，再复制活动文件。保留原目录，不执行删除或移动。
4. 多份旧数据分别备份，第一份作为活动数据，其余不混入记忆。界面显示额外旧数据备份数量，可从 `.backups` 查看。
5. 迁移中断保留 pending journal 和原始 ZIP，下次启动验证后恢复。遇到不同内容的已存在文件则保留原文件并停止迁移，避免静默覆盖。完成标记使迁移幂等；之后删除的记忆不被旧目录“复活”。
6. 微信配置中指向被迁移旧目录内部的绝对路径，在运行时映射到新目录；用户自行选定的其他外部微信状态目录不改写。

先退出旧版本再启动新版，避免两个进程同时修改同一套数据。磁盘/权限/校验错误不会回退为一套空白配置；旧文件保持完整，可以根据本地备份排查恢复。

## 本地备份与恢复

「模型与行为 → 本地数据与备份」显示实际目录，桌面版可打开目录，浏览器版可复制路径。点击「创建本地备份」生成 `.backups/wxbot-backup-...zip`，校验通过后提供下载链接。创建/下载都经过现有管理认证及同源检查，下载响应 `Cache-Control: no-store`。备份含真实密钥和聊天资料，仅保存在本地，不自动上传。

备份排除历史备份目录、迁移控制文件、临时写入文件与链接文件，避免递归备份或拷贝其他位置的文件。用户自定义的外部微信状态目录作为 `external/ilink/` 单独包含，清单记录来源。对单文件读取进行前后 stat 检查，文件变化会最多重试三次；为获得严格的整套同一时刻状态，先停止机器人再备份。

恢复到新设备/新用户：先退出应用；创建默认用户数据目录；把 ZIP 中的数据文件恢复到该目录，保留当前文件的额外备份，不把 `__wxbot_backup_manifest__.json` 当成应用状态。外部微信目录单独恢复并更新其配置路径。重新启动后沿用人格与记忆；平台可能仍要求重新确认登录。恢复前可校验：

```sh
python -c 'from pathlib import Path; from src.core.storage import verify_backup; print(len(verify_backup(Path("BACKUP.zip"))["files"]))'
```

## Docker

Compose 把宿主机 `${HOME}/.local/share/wxbot/user-data` 映射到容器 `/app/data`，不再把活动资料放在项目内。通过 `WXBOT_HOST_DATA_DIR` 可设其他持久位置。旧 `./data` 只读挂到 `/app/legacy-data`，空的新目录首次启动时复制迁移；原文件仍留在项目里，迁移后的独立目录不随项目删除。

Windows Compose 用户建议先设置绝对 `WXBOT_HOST_DATA_DIR`，例如 PowerShell：

```powershell
$env:WXBOT_HOST_DATA_DIR = "$env:LOCALAPPDATA\WxBot\user-data"
docker compose up -d
```

仅重建镜像/容器不影响宿主机绑定目录。若以前自定义过卷位置，继续显式绑定原位置或指定迁移来源，不切换到空目录。

官方路径/安装行为依据：
- https://www.electronjs.org/docs/latest/api/app#appgetpathname
- https://www.electron.build/docs/nsis/
- https://docs.docker.com/reference/compose-file/interpolation/

源码测试覆盖固定路径、非覆盖迁移、中断恢复、旧文件保留、模拟升级/卸载/重装、备份校验与恢复、权限和认证。v2.1.3另外完成Windows Setup真实安装/卸载/重装及测试数据保留；临时夹具不代表用户真实数据已备份或恢复。
