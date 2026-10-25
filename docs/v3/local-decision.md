# 接话判定：轻量云端策略与已退役本地实验

更新：2026-10-23。**当前应用已弃用本地模型；以下本地训练命令和数字仅作历史研究归档，不是安装步骤或当前发行前置条件。**

## 当前产品路径

明确问题、请求、更正、呼唤和情绪诉求由保护规则直接保障回应。只有模糊私聊发起轻量云判定：默认`deepseek-flash`非思考模式、短`R/D/C`协议、总期限3秒；异常保守回应，不将判定诊断发给聊天用户。判定不读取人格全文或知识库。等待窗口约0.8秒防抖后可并行预判；同一待回复连续输入批次最多开放一次云判定额度，真正调用前扣除，防抖取消不扣。相同有效结果只交接一次；新来话使旧版本失效但不重开额度，最新全文仍先走保护/确认/未完规则，模糊则保守回应，诊断标记`conservative / burst_budget_exhausted`，不称为云命中。独立判定配置、主模型/Key与主回复fast/deep保持可配置。新批次与独立稍后重判不永久共用旧额度，详细边界见[调用与费用核查](cache-cost.md)。

运行与安装包不再导入、加载、预热或下载ONNX模型，不依赖ONNX Runtime/tokenizers。旧工具、源码、模型和报告保留在研究位置，不自动清理；缺模型或历史质量门失败不阻止应用构建。当前会话与真实缓存计量见[稳定会话与KV缓存](context-session.md)，发布状态见[验收记录](validation.md)。

## 历史研究：实现与来源

- [Chinese-Jev](https://github.com/gulucaptain/Chinese-Jev)：借鉴中文训练与校准流程，记录源码版本 `f4138c028e0da30c40e7e32aa294a38b817b694d`；不使用其医疗专用权重。
- [Laya-multilingual](https://huggingface.co/convaiinnovations/laya-multilingual)：底座固定 revision `1720e3e3357cfe1e281542e223f8273b0890ca34`，原始 `model.safetensors` SHA256 为 `9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204`。
- [Laya源码](https://github.com/NandhaKishorM/laya/tree/d74e8be46ef5241e4ba2b16cd25e896f24f71373)：开发代码来源版本固定。当前项目训练最后四层encoder与项目固定三分类头；不是直接把上游零样本输出当成中文质量验收。
- 标签固定为 `reply`、`defer`、`context_only`。模型读取本轮及短历史状态，不读取人格全文或长期知识库；超长输入弃权，不截断后静默。

历史实验训练使用Torch，研究包由ONNX、tokenizer、policy、校准和来源/哈希清单组成；这些文件不再进入产品安装包。动态INT8采用 per-tensor MatMul，不做per-channel。底座embedding等仍可能为FP32，不能从“INT8”推断整个模型或安装包已缩小四倍。

## Windows 为主要训练与验收平台

项目主要面向Windows用户。历史研究在远端 **原生Windows x64** 完成续训、量化后的质量回归和CPU测速；GPU用于训练，研究包以CPU ONNX工作进程作性能对照，不代表当前应用运行路径。macOS 历史数字保留为辅助对照，不用于推断 Windows 结果。已观察的硬件、定向测试、固定输入和本轮计划见[Windows训练记录](windows-training.md)；其中未完成阶段明确标记待实测。

运行入口：[train-windows.ps1](../../tools/decision_model/train-windows.ps1)。脚本只编排开发流程，不安装依赖、不改驱动、不重启机器，不将实验权重复制进发行目录。源码、语料和模型分别保存在不同位置；不读取用户聊天目录或登录文件。

### 独立环境与新实验

先在 Windows 构建机准备两个独立 x64 Python 3.10+ 虚拟环境：

- `TrainPython`：Torch（对应实际 GPU 架构的 Windows CUDA 构建）、transformers、safetensors、huggingface_hub、scipy、onnx，以及导出/量化所需依赖。固定版本参考 [训练依赖](../../tools/decision_model/requirements-training.txt)，实际锁定版本写入每次运行的 `training-environment.json`；已有 CUDA 环境不要盲目重装或降级。固定 Torch 2.8.0 的 Windows CUDA 12.8 wheel 参考 [PyTorch 官方历史版本安装说明](https://pytorch.org/get-started/previous-versions/)；其他版本也须固定实际来源和哈希。CUDA 可用还需一次真实张量运算验证，不只看显卡型号或 `nvidia-smi`。
- `RuntimePython`：仅 CPU `onnxruntime`、`tokenizers`、`numpy` 等运行依赖，不安装 Torch。预检会拒绝含 Torch 的运行环境，确保性能不借用训练框架或 GPU；它不是最终冻结安装包本身。

下面的路径都是操作方自定的示例，不包含远端地址或凭据。`WorkspaceRoot` 是源码目录；缓存、语料、每次运行输出必须位于源码目录外，支持中文和空格路径。

```powershell
$Train = 'D:\WxBot Training\train-venv\Scripts\python.exe'
$Runtime = 'D:\WxBot Training\runtime-venv\Scripts\python.exe'
$Code = 'D:\WxBot Source'
$Cache = 'D:\WxBot Training'
$Data = Join-Path $Cache 'corpus-v3'
$Base = Join-Path $Cache 'base-laya-pinned'

& (Join-Path $Code 'tools\decision_model\train-windows.ps1') `
  -WorkspaceRoot $Code -CacheRoot $Cache -DataRoot $Data `
  -OutputRoot (Join-Path $Cache 'runs') `
  -TrainPython $Train -RuntimePython $Runtime -BaseDir $Base `
  -RunName 'windows-cuda-experiment-001' -Device cuda `
  -Epochs 3 -Batch 8 -LearningRate 0.00004 -Seed 215
# 保存实际退出码；非零时检查对应run的run-state.json和阶段日志。
$code = $LASTEXITCODE
Write-Output "training_exit=$code"
```

已有 `DataRoot` 必须包含原始 `train.jsonl`、`calibration.jsonl`、`test.jsonl` 和 `dataset.json`。只有明确从生成器新建研发语料时才加 `-PrepareData`，且目标目录必须尚不存在；它不覆盖已有校准集或测试集。扩大训练集应新建带来源/哈希的语料目录并保留锁定的校准/测试文件，不把反复使用的开发回归称为新盲测。`BaseDir` 用已下载的固定底座目录，pipeline校验底座 SHA；省略时才使用开发缓存下载。

默认 `Device=cuda`，缺失 CUDA 或实际算子不可执行即退出，不悄悄换 CPU。显式 `-Device cpu` 才用于 CPU 训练对照；`auto` 必须从报告确认真正选到的设备。脚本会记录操作系统、Python、依赖版本、CUDA、显卡能力、源码与语料 SHA256。

### 从旧训练结果继续

`Resume` 指向旧 `task-head.safetensors`，旁边必须保留原 `training.json`。它是加载既有权重继续微调，不是恢复完整 AdamW 优化器或中断批次；种子和优化器会重新初始化。`Epochs` 表示本次追加轮数。pipeline 会校验原底座/来源/架构；旧实验和检查点只读，每次仍使用**新 RunName**。

```powershell
& (Join-Path $Code 'tools\decision_model\train-windows.ps1') `
  -WorkspaceRoot $Code -CacheRoot $Cache -DataRoot $Data `
  -OutputRoot (Join-Path $Cache 'runs') `
  -TrainPython $Train -RuntimePython $Runtime -BaseDir $Base `
  -RunName 'windows-cuda-experiment-002' -Device cuda `
  -Resume (Join-Path $Cache 'runs\windows-cuda-experiment-001\task-head.safetensors') `
  -Epochs 2 -Batch 8
```

顺序为：环境预检 → 原样固定输入 → 训练 → 校准 → per-tensor 动态INT8导出并重校准 → **独立 RuntimePython CPU benchmark** → 质量评估 → package-check。评估本身也使用 CPU ONNX，但由开发环境执行，以包含完整评测依赖。每阶段非零立即停止，保留全部已完成产物；质量失败返回2，故失败实验也已保留独立运行环境的 `benchmark-windows-cpu.json`。

每个新输出目录包含 `run-state.json`、`00-…09-*.log`、两份环境 JSON、输入/源码哈希、各 epoch 检查点及原pipeline产物。重复 RunName 立即拒绝，不自动删除旧run。完成也只代表该研发流水线结束；`release_accepted` 始终为false，不能绕过独立锁定会话集、安装包和跨平台验收。质量失败或还没有 Windows 原生实测时，不捆绑实验模型、不填写推测成绩。

## Windows运行策略候选：独立复测

本轮CUDA训练和基准INT8导出已经完成，但Windows CPU原2线程基线的256-token p95为1397.9373ms，未达300ms目标。一次P核子进程30次对照p50=212.57ms、p95=216.55ms、最大217.43ms仅用于诊断，**不是100次验收或候选质量通过**。FTZ基本无效、关闭spinning更慢，均不改为默认；原始记录及完整边界见[Windows训练记录](windows-training.md)。

历史worker已加入可选 `prefer_performance_cores`，不硬编码该机器的mask或核号，只约束当前worker自身进程，不改变系统、父进程或其他进程。当时使用独立候选目录，按[运行策略候选工具](../../tools/decision_model/tune_runtime.py)实际支持的policy创建4线程加P策略候选，重新运行原1998条校准。候选创建/拟合仍属研发环境，不给发行环境增加Torch或scipy；模型与tokenizer保持原字节，旧evaluation及pass状态丢弃。

完成实际worker策略下的重校准后，才执行如下已支持的复测命令。`$Candidate` 指向新候选run，`$Runtime` 是不含Torch的独立运行环境，`$Train` 是含完整评测依赖的研发环境；两个命令各启一个新进程，不能在一个进程里反复切FTZ来比较：

```powershell
& $Runtime -m tools.decision_model.benchmark `
  --model-dir (Join-Path $Candidate 'package') `
  --output (Join-Path $Candidate 'benchmark-windows-cpu.json') --repeats 100
& $Train -m tools.decision_model.pipeline evaluate --data $Data --out $Candidate
```

benchmark与评测都使用真实worker里的相同选项；保存CPU provider/线程、有效P核选择或弃权原因、policy/模型/校准哈希，避免仅在测量脚本绑核获得结果。4线程加P策略与Gather UINT8候选现已分别完成重校准、100次CPU复测和开发质量回归，详细结果保留在[Windows训练记录](windows-training.md)。这些研究结果不作为当前云判定的质量或时延。

发行来源验证已加强：export只从真实训练记录绑定训练split及checkpoint/元数据SHA，evaluate还要求runtime校准SHA与当前calibration一致。缺训练来源、未验证的resume历史或错误split会保留开发诊断但关闭独立门，不从后来的新dataset反推历史。本轮合成开发回归仍不具盲测资格，调整运行策略不改变这一点。

## 其他开发平台的可复现命令

在仓库根目录运行，训练产物使用源码目录以外的缓存。开发训练环境需另装Torch、transformers、safetensors、huggingface_hub、scipy、onnx及运行时依赖；**这些训练依赖不加入用户运行依赖**。固定版本与设备应一并写入最终报告，下面不隐式安装或替换本机环境。前一轮 macOS 训练缓存使用Python 3.13.5，依赖版本记录如下（仅历史复现，不是 Windows CUDA 环境或用户安装要求）：

```text
torch==2.8.0
transformers==4.57.6
safetensors==0.6.2
huggingface-hub==0.36.2
scipy==1.18.1
onnx==1.19.1
onnxruntime==1.23.2
tokenizers==0.22.2
numpy==2.2.6
```

```sh
# PYTHON 指向已准备训练依赖的独立虚拟环境。
PYTHON="$HOME/.cache/wxbot-decision-v3/venv/bin/python"
CACHE="$HOME/.cache/wxbot-decision-v3"
DATA="$CACHE/corpus"
RUN="$CACHE/reproduce-fixed-choice"

"$PYTHON" -m tools.decision_model.pipeline prepare --out "$DATA"
"$PYTHON" -m tools.decision_model.pipeline fetch --cache "$CACHE" --out "$RUN"
"$PYTHON" -m tools.decision_model.pipeline train \
  --cache "$CACHE" --data "$DATA" --out "$RUN" \
  --epochs 3 --batch 8 --lr 0.00004 --seed 215
"$PYTHON" -m tools.decision_model.pipeline calibrate --data "$DATA" --out "$RUN"
"$PYTHON" -m tools.decision_model.pipeline export --cache "$CACHE" --data "$DATA" --out "$RUN"
"$PYTHON" -m tools.decision_model.pipeline evaluate --data "$DATA" --out "$RUN"
# 仅合格清单才应得到 LOCAL_DECISION_PACKAGE PASS；失败退出不是可发布结果。
"$PYTHON" -m tools.decision_model.pipeline package-check --out "$RUN/package"
```

首次获取上游底座需要联网，仅在显式执行历史研究工具时发生。应用启动和打包不调用这些命令；旧`WXBOT_LOCAL_DECISION_DIR`不再由桌面应用传入，也不启用产品本地推理。研究包自身仍保留manifest、哈希与质量校验，失败状态不手动改写。

输出包括：`training.json`、`calibration-logits.json`、`calibration.json`、`export.json`、`evaluation.json`、`predictions.json`，以及 `package/` 内的运行资源。保留失败实验，不手动改写 `quality_gate.passed`。

## 数据集与独立性

首轮确定性合成语料由 `tools/decision_model/corpus.py` 生成，seed 215，为7200条训练、1998条校准、1998条测试。新一轮Windows训练侧扩展为9020条，旧校准/测试保持不变，具体哈希与资格见[Windows训练记录](windows-training.md)。按不重叠的表达模板家庭划分，实体可以共用；SHA256和家庭清单记录于 `dataset.json`。

语料由AI编写，不是人工标注语料，不含用户私聊，不代表真实用户总体分布。当前分割以表达模板家庭为单位，尚不是来自独立真实会话的样本。该测试已用于多轮开发比较，应作为开发回归集；当时拟议的本地模型发布还需冻结新的独立会话级锁定集，避免反复调参后继续称其为盲测。

需要额外覆盖中文口语、繁体、中英混输、反讽、引用、情绪、撤销、更正、长输入及连续补充；不能只靠清晰模板达到门槛。保护规则的回归、模型自身保护切片与整条应用链路应分别报告。

## 历史本地实测：未通过

`run-v3-fixed/evaluation.json`，macOS arm64 CPU ONNX，1998条合成测试：

| 指标 | 实测 |
|---|---:|
| 三分类准确率 | 77.6276% |
| macro-F1 | 0.774633 |
| ECE | 0.074940 |
| 本地接纳覆盖率（全部测试，不等同模糊消息切片） | 55.0050% |
| 接纳静默 | 780条，其中92条类别错误 |
| 接纳静默精确率 | 88.2051% |
| 模型保护切片 | 666条，16条被错误接纳为静默 |
| 暖机p95（该短文本测试分布） | 13.0205毫秒 |
| ONNX文件大小 | 898,151,671字节，不含tokenizer/校准等 |

测试集SHA256：`d346acc12a0ed2cd7d66446ffb0e009183c97102bdb06df6ac9331f6937c6841`。

该数字不能宣称Windows/macOS三平台256-token速度、静默质量或实际聊天体感已经通过。应用层另有保护规则并保守处理异常，原始模型保护切片的16条错误不等于已观察到16次真实用户漏答，但仍足以阻止发行该模型。

## macOS真实长度性能补测

另用单条输入、CPUExecutionProvider、2线程，各100次暖机后测量；这是同一未通过质量门的研发权重，**不放行发行**：

| 输入长度 | p50 | p95 |
|---|---:|---:|
| 32 token | 12.38ms | 13.28ms |
| 128 token | 48.27ms | 49.33ms |
| 256 token | 108.27ms | 110.07ms |

含哈希与加载的冷启动约1284.90ms；整个测量进程峰值RSS为3,117,236,224字节，模型目录932,518,474字节。峰值包含测量/加载阶段，不宣称都是纯推理常驻内存；目录大小不是压缩后安装包增量。长输入tokenize夹具p95约0.61ms。

该历史补测满足当时本机256-token速度目标；Windows现已完成基线及真实worker优化复测：后者本机256-token p95为225.70ms，但质量仍未通过，见[Windows训练记录](windows-training.md)，其他macOS架构仍未测试；短句13ms只保留为对应输入分布的测量，不作统一速度标称。完整端到端速度还包含收集、图像、搜索、LLM生成、平台投递，须另测。

## 历史模型验收要求（当前发行已不适用）

退役前方案要求验证：保护切片零误静默、自动静默精确率≥99%、应答召回≥99.5%、模糊消息本地覆盖≥50%；三平台256-token暖机p95目标≤300毫秒，同时记录冷启动、内存和真实包增量。校准集用于拟合阈值，测试集不参与阈值选择；量化后重新校准，固定源码、权重及产物哈希。

研究评测器保留上述质量门及macro-F1/ECE等诊断；旧数据缺少的独立性证据不补造。应用已改用云判定，不再发布含此试验模型的安装包，也不以其训练或质量结果阻塞当前应用发行。
