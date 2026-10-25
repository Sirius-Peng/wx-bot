# WxBot third-party notices / 第三方组件说明

更新：2026-10-25。此文件说明 **v3.0.0 发行组成与来源**，不表示所有平台安装包已完成构建或发布。精确运行依赖版本与所带许可证以每个平台实际构建生成的 `third-party/manifest.json` 和对应原文为准。

WxBot 项目版权与许可保留于根目录 `LICENSE`：Copyright (c) 2026 Yuhan Peng，MIT License。GitHub 作者为 [Sirius-Peng](https://github.com/Sirius-Peng)。第三方组件仍归其作者所有；项目的 MIT 声明**不覆盖整个安装包中的第三方组件、Live2D SDK 或用户导入的模型**。

## 安装包中的声明位置

- 应用资源目录根部的 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`：本项目MIT原文及本说明。
- `third-party/manifest.json`、`third-party/licenses/<package>/` 与 `third-party/PYTHON-LICENSE.txt`：实际 Python 运行依赖、其随附/补充许可证、组件版本、SHA256清单及Python许可。
- `web/live2d/`：原始 `CORE-LICENSE.md`、`FRAMEWORK-LICENSE.md`、`SDK-LICENSE.md` 及渲染资源 `manifest.json`，保持上游名称与内容。
- Electron 原生运行时随附的 `LICENSE`、`LICENSES.chromium.html` 等文件：保留 Electron/Chromium 及其内嵌组件的声明。不同操作系统的原生目录布局不同。

这些文件应与真正分发的应用资源一起保留；不能仅以一个网页链接取代已随组件提供的许可原文。版本清单仅覆盖被收集的运行依赖，不把开发环境中训练或打包工具的全部依赖都当成应用组件。

## Python 运行依赖

| 组件 | 用途 | 上游与许可标识 |
|---|---|---|
| CPython | 冻结 Python 运行时 | [Python](https://www.python.org/)，PSF License，包含其随附声明 |
| FastAPI / Pydantic | 本地管理 API、配置校验 | [FastAPI](https://github.com/fastapi/fastapi)、[Pydantic](https://github.com/pydantic/pydantic)，MIT |
| Starlette / Uvicorn | HTTP/WebSocket 服务 | [Starlette](https://github.com/Kludex/starlette)、[Uvicorn](https://github.com/Kludex/uvicorn)，BSD-3-Clause |
| AnyIO | 异步运行支持 | [AnyIO](https://github.com/agronholm/anyio)，MIT |
| HTTPX / HTTP Core | 网关与模型 HTTP 连接 | [HTTPX](https://github.com/encode/httpx)、[HTTP Core](https://github.com/encode/httpcore)，BSD-3-Clause；实际 SDK 可能另带 httpx2 等运行依赖，按构建清单记录 |
| OpenAI Python SDK | 兼容模型接口调用 | [openai-python](https://github.com/openai/openai-python)，Apache-2.0；保留实际包内 vendor 许可，包括存在时的 `openai/_vendor/httpx_aiohttp/LICENSE` |
| Certifi | HTTPS CA 证书集合 | [Certifi](https://github.com/certifi/python-certifi)，MPL-2.0，保留证书来源及随附许可 |
| tzdata | 跨平台 IANA 时区数据 | [python/tzdata](https://github.com/python/tzdata)，保留其 Apache-2.0 与时区数据随附说明 |
| Jieba | 本地中文分词与词频统计 | [fxsjy/jieba](https://github.com/fxsjy/jieba)，MIT；固定版本许可来源见下一节 |
| Cryptography | 微信媒体解密 | [cryptography](https://github.com/pyca/cryptography)，Apache-2.0 OR BSD-3-Clause，包含其二进制库随附声明 |
| Pillow | 图像解码与归一化 | [Pillow](https://github.com/python-pillow/Pillow)，MIT-CMU，保留实际构建版本的原始 LICENSE 与内嵌库说明 |

传递依赖、二进制依赖和实际版本以构建时清单及随附原文为准，不仅限于此表。PyInstaller 是构建工具，其运行时/bootloader 按其 GPL 许可及分发例外处理，不把例外误写为整个工具 MIT。

### Jieba 0.42.1 原文补充

部分 Jieba wheel 未带许可证文件，因此本仓库保留上游固定版本原文 `desktop/licenses/jieba-MIT.txt`，随应用复制至 `third-party/licenses/jieba/`，实际文件名及摘要由 `third-party/manifest.json` 记录。

- 上游版本：`v0.42.1`
- 固定提交：`1e20c89b66f56c9301b0feed211733ffaa1bd72a`
- 原文：[LICENSE](https://github.com/fxsjy/jieba/blob/1e20c89b66f56c9301b0feed211733ffaa1bd72a/LICENSE)
- Git blob：`9d7e66b431461c785329a1b52199d4207daefacc`
- 文件长度：1075 bytes
- SHA256：`18ba0984839f85853b29fadaf992f7dba8fd0ca0fbeae34de2b8735222dc7a37`

许可文件本身保持逐字节原样；以上来源与校验记录单独保留，不插入或改写许可正文。

## Electron 与 Live2D

[Electron](https://github.com/electron/electron) 采用 MIT；其 Chromium/Node.js 及其他组件各自许可随 Electron 运行时保留。Electron Builder、esbuild 等仅开发/打包工具不因此整套打入应用。

Live2D 渲染资源来自官方 **Cubism SDK for Web 5-r.5**，Core 标识 **06.00.0001**。资源来源及摘要记录于 `web/live2d/manifest.json`。本项目未将其重新许可为 MIT：

- Cubism Core：[Live2D Proprietary Software License](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html)。
- Cubism Framework / Components：[Live2D Open Software License](https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html)。
- SDK 及发行：[Cubism SDK Release License](https://www.live2d.com/en/download/cubism-sdk/release-license/)，适用条件以对应上游条款为准；仓库保留 SDK 附带说明，不宣称已获独立商业授权。

应用默认形象是本项目的静态标记；**不随包分发 Haru 或其他 Live2D 示例/角色模型**。用户自行导入的模型保存在独立用户数据目录，模型、贴图、动作和音乐等资源的许可仍来自模型提供者，不由本项目 MIT 声明替代。

## 不进入正式安装包的研究组件

Chinese-Jev / Laya 流程、历史权重、Torch、Transformers、ONNX Runtime、tokenizers、numpy 等仅保留在独立训练研究中，不作为当前云判定的运行依赖，不随应用下载或预热。源代码中的研究 vendor 继续保留其 Apache LICENSE 与 NOTICE；不把研究通过与否当作当前应用发行结论。

## 机制参考与外部服务

SillyTavern、Open-LLM-VTuber、LangMem、Letta、Graphiti 与 OpenClaw 作为人格/上下文/记忆和队列机制参考，不表示其维护者认可本项目。腾讯 iLink、NapCat/OneBot 网关及 DeepSeek 等云接口属于外部接入，不把它们的服务或品牌归入本项目 MIT 许可；外部网关和服务的获取、账号与条款仍由各上游提供。
