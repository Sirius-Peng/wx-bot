# v3 第三方来源与发行组件声明

更新：2026-10-28。发行组件说明见根目录 [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md)。本页保留研究与渲染资源的详细来源；实际运行依赖及版本、许可证原文、SHA256以每个平台构建生成的 `third-party/manifest.json` 及其 `third-party/licenses/` 原文为准，**不表示本页更新时所有平台安装包已发布**。项目作者与仓库仍为[Sirius-Peng / wx-bot](https://github.com/Sirius-Peng/wx-bot)；第三方组件不改列为本项目作者。

## 已退役模型与训练研究（不进入应用安装包）

| 来源 | 本项目用法 | 固定标识/待办 |
|---|---|---|
| [Chinese-Jev](https://github.com/gulucaptain/Chinese-Jev) | 借鉴中文训练、校准与类型化判定流程，不引入其医疗权重 | 源码 `f4138c028e0da30c40e7e32aa294a38b817b694d`；保留研究来源说明 |
| [Laya源码](https://github.com/NandhaKishorM/laya) | 开发用vendor模块及Laya encoder加载，项目另训固定三分类头 | 源码 `d74e8be46ef5241e4ba2b16cd25e896f24f71373`；源码已保留[Apache LICENSE](../../tools/decision_model/vendor/LICENSE)与[NOTICE](../../tools/decision_model/vendor/NOTICE)；源代码再分发仍应保留原声明；不打入应用包 |
| [Laya-multilingual模型卡](https://huggingface.co/convaiinnovations/laya-multilingual) | 历史底座权重，只用于独立研究，不随应用提供 | revision `1720e3e3357cfe1e281542e223f8273b0890ca34`；缓存模型卡标明Apache-2.0，研究再分发应核对对应条款；当前应用包不包含权重 |
| Torch / transformers / onnx 等 | 仅开发训练、导出使用 | 不进入用户训练框架运行路径；开发环境版本应写入复现报告 |
| ONNX Runtime / tokenizers / numpy | 仅历史研究中的CPU推理 | 已从根运行依赖移除，PyInstaller排除，不在应用包内提供；版本记录保留于研究报告 |

当前产品使用已配置的云LLM，不加载或下载以上模型及训练/推理框架；历史工具和外部权重不自动删除，研究质量门不再阻止应用构建。实际应用分发按最终资源清单保留HTTP/Web/图像/词典及Live2D等组件的许可证。Python构建收集仅覆盖实际运行依赖，不将训练环境整体复制入包；Jieba 0.42.1 wheel缺少原文时由固定上游许可补充，版本、来源与摘要见根第三方说明。

试验数据为AI编写的合成示例，不宣称上游提供了该中文接话数据集，也不宣称人工标注或真实用户质量。训练/校准/测试与失败实验说明见[本地决策](local-decision.md)。

## Live2D

当前渲染资源由官方[ Cubism SDK for Web 5-r.5压缩包](https://cubism.live2d.com/sdk-web/bin/CubismSdkForWeb-5-r.5.zip)构建，Core版本`06.00.0001`；构建脚本为`desktop/scripts/build-live2d.cjs`，本地SDK目录由`WXBOT_CUBISM_SDK`指定。用户启动不下载SDK或角色模型。

仓库已保留SDK附带的原始许可说明：

- [Core说明](../../src/web/live2d/CORE-LICENSE.md)：Live2D Proprietary Software License。
- [Framework说明](../../src/web/live2d/FRAMEWORK-LICENSE.md)：Live2D Open Software License及SDK发行相关说明。
- [SDK说明](../../src/web/live2d/SDK-LICENSE.md)：SDK、示例与素材的条款说明。
- [资源manifest](../../src/web/live2d/manifest.json)：SDK来源及Core/renderer摘要。

这些是独立第三方条款，不因desktop/package.json中的MIT字段而改为MIT。最终安装包在 `web/live2d/` 保留以上原说明，并在资源根部保留项目 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`，实际Python运行依赖原文和清单置于 `third-party/`。具体发行方式及模型使用仍遵循对应上游条款，本说明不替代上游条款，也不宣称整个发行包均为MIT。

默认形象为本项目页面内的静态标记，不分发SDK示例Haru或其他角色模型。开发测试可从已下载的官方SDK中导入示例，测试使用不表示该示例自动成为产品默认资源。用户自行导入的模型存于个人数据目录，模型本身的使用、署名和再分发条件仍来自模型提供者。

## 微信角色语音与外部服务

v3.0.2 可选使用用户配置的 Fish Audio 服务及公开的弗洛洛社区音色；服务 Key、额度与模型引擎独立于聊天 LLM。此音色由社区提供，不是本项目新训练或随包分发的权重，也不表示音色提供者或游戏方为本项目背书。默认音色标识、官方接口及备选语料来源见[语音来源核对](voice-sources.md)。

应用只提供 HTTP 接入、配置和音频附件投递，不在安装包中复制游戏音频、角色参考 WAV、GPT-SoVITS 角色权重或训练框架。可选本地 GPT-SoVITS 连接的是用户另行运行的服务，其代码许可与角色素材条件分别适用，不将第三方素材归为本项目 MIT 资源。真实合成音质、付费调用和微信客户端播放不因打包成功而记为已验证。

## 机制参考，不引入其托管服务

- [SillyTavern](https://github.com/SillyTavern/SillyTavern)：稳定角色提示与有界上下文的机制参考。
- [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)：桌面形象、事件生命周期与模型资源分离的机制参考。
- [LangMem](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)、[Letta](https://docs.letta.com/configuration/memory)、[Graphiti](https://github.com/getzep/graphiti)：后台整理、来源、时间有效性参考。
- [OpenClaw](https://github.com/openclaw/openclaw/blob/main/docs/concepts/queue.md)：按会话排队和续话处理参考。

本项目不以这些名称暗示其维护者认可本项目，不引入其托管记忆服务、向量数据库或额外服务Key。若后续实际复制代码，应按复制时固定版本补全具体文件、许可及修改记录，而不是只保留机制参考链接。
