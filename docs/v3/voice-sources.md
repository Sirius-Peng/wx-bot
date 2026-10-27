# 弗洛洛语音：来源与接口核对

核对日期：2026-10-27。资源链接与接口配置不等于本项目已经完成角色训练、音色质量验收或真实微信投递。

## 当前选用：Fish Audio 社区音色

- 音色页：[弗洛洛](https://fish.audio/zh-CN/m/5b432f747d00419aba7cb12fa50e4af0/)。`reference_id` 为 `5b432f747d00419aba7cb12fa50e4af0`。这是一项已存在的社区音色，不是本项目训练的新模型。
- 本次选择直接调用 Fish 服务，不安装本地训练框架，也不自动下载游戏音频或角色权重。
- 使用用户自己的 Fish API Key。Key 在 [Fish 控制台](https://fish.audio/app/api-keys) 获取；文字回复所用的 DeepSeek Key 不可代用。平台 API 用量与聊天模型用量分开计算，账户价格、额度和账单以 Fish 平台为准。
- [官方 TTS 文档](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech)确认 JSON 请求、`reference_id` 和 WAV 输出。当前支持的 `model` 请求头包括 `s1`、`s2-pro`、`s2.1-pro`、`s2.1-pro-free`、`drama-3`；本项目默认显式传 `s2.1-pro`。
- 官方说明省略或传入未知模型名会回退到 `s2.1-pro`。配置应校验模型名，不把拼写错误或请求失败当作切换付费模型的理由；也不把模型名称带 `free` 当作一定有可用额度的保证。

以下为接口示意，不包含真实 Key，也不会因为查看文档而执行：

```sh
curl --fail-with-body https://api.fish.audio/v1/tts \
  -H "Authorization: Bearer $FISH_API_KEY" \
  -H "Content-Type: application/json" \
  -H "model: s2.1-pro" \
  --data '{"text":"这段旋律，你听到了吗？","reference_id":"5b432f747d00419aba7cb12fa50e4af0","format":"wav","sample_rate":24000}' \
  --output phrolova-example.wav
```

WAV 是合成接口的输出格式，不代表可以原样塞入微信语音消息。微信传输格式、转码、上传和可靠投递由适配器另行处理。语音生成或上传失败不应引起第二次文本回复，也不应自动切换供应商重复计费。

## 已核实的中文训练语料与角色权重

这些是供后续研究或用户自行获取的备选来源，本次 Fish 路线不依赖它们。

| 资源 | 实际核对结果 | 入口 |
| --- | --- | --- |
| 弗洛洛中文原始语料 | 角色目录存在 `弗洛洛.7z`；下载地址 HEAD 最终返回 HTTP 200，长度 **15,127,194 bytes**（约14.43 MiB） | [AI Hobbyist 资源库](https://res.acgnai.top/) → 数据集 → 鸣潮 → 中文 → 弗洛洛 |
| 弗洛洛中文 GPT-SoVITS 模型 | 目录存在 `弗洛洛_ZH.zip`；HEAD 返回 HTTP 200，长度 **213,483,056 bytes**（约203.59 MiB） | [ModelScope 角色模型文件](https://modelscope.cn/models/aihobbyist/GPT-SoVITS_Model_Collection/resolve/master/%E9%B8%A3%E6%BD%AE/%E4%B8%AD%E6%96%87/%E5%BC%97%E6%B4%9B%E6%B4%9B_ZH.zip) |

语料库入口由[发布者仓库](https://github.com/AI-Hobbyist/WutheringWaves_Datasets)及[数据卡](https://modelscope.cn/datasets/aihobbyist/WutheringWaves_Dataset/resolve/master/README.md)提供。2026-10-27 的资源库只读目录响应实际包含上述角色，而不是仅凭仓库旧版 README 推测。目录显示角色存在，不等于确认覆盖某个游戏版本的全部台词或每条音频都有准确转写。

仅以一个 65,536-byte HTTP Range 读取了角色 ZIP 的目录：包内列有 `v4/`、GPT `.ckpt`、SoVITS `.pth`、`reference_audios/` 中的 WAV 和训练日志。没有下载完整权重、加载权重或执行训练；文件存在不等于声音相似度已经验收。

**原始语料、训练权重、参考音频和在线音色 ID 不是同一种资源。** 原始语料用于整理和训练；本地权重依赖对应推理环境；参考音频还需要匹配的转写；Fish 的 `reference_id` 则指向其在线服务音色。英语 RVC 语音转换模型也不是中文 GPT-SoVITS TTS 的直接替代品。

## 来源声明与本项目打包方式

[角色模型卡](https://modelscope.cn/models/aihobbyist/GPT-SoVITS_Model_Collection/resolve/master/README.md)列出 CC-BY-NC、训练参与者署名及禁止二次配布等条件，训练参与者包括红血球AE3803与白菜工厂1145号员工。数据卡虽列出 GPL3.0 元数据，同时另指向发布者的使用说明；这不应被表述成游戏原始音频可以任意再分发。

本项目只记录来源和提供用户配置/导入接口，不把这些角色音频、参考 WAV 或权重复制进安装包，也不把 GPT-SoVITS 代码的 MIT 许可当作角色素材的许可。

## 本地服务的上游接口参考

备选的[GPT-SoVITS 上游](https://github.com/RVC-Boss/GPT-SoVITS)提供独立的 [HTTP API v2](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)。`POST /tts` 可传 `text`、`text_lang`、`ref_audio_path`、`prompt_text`、`prompt_lang`，并请求 `media_type=wav`、`streaming_mode=false`。它不要求云端 Key，但服务端须已有匹配的运行环境、模型和参考文件；`ref_audio_path` 指服务端路径，不是任意客户端路径。

上游训练标注格式为 `vocal_path|speaker_name|language|text`，与直接将现成人格文本当成语音训练集不同。本次不启动该备选服务，不将其依赖加入桌面软件的基础运行环境。
