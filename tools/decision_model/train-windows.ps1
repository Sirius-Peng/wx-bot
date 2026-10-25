#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$WorkspaceRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$CacheRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'WxBot\model-training\v3'),
    [string]$DataRoot = '',
    [string]$OutputRoot = '',
    [string]$TrainPython = '',
    [string]$RuntimePython = '',
    [string]$BaseDir = '',
    [string]$RunName = '',
    [string]$Resume = '',
    [ValidateSet('cuda', 'cpu', 'auto')][string]$Device = 'cuda',
    [ValidateRange(1, 1000)][int]$Epochs = 3,
    [ValidateRange(1, 1024)][int]$Batch = 8,
    [ValidateRange(0.00000001, 1.0)][double]$LearningRate = 0.00004,
    [ValidateRange(0, 2147483647)][int]$Seed = 215,
    [switch]$PrepareData
)

# Development orchestrator only. It never installs packages/drivers, copies a
# research model into desktop resources, changes a quality gate, or removes runs.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$script:RunState = $null
$script:RunDir = $null
$script:ExitCode = 1
$OriginalLocation = Get-Location

function Write-Utf8Json([string]$Path, $Value) {
    $text = ConvertTo-Json -InputObject $Value -Depth 32
    $temporary = $Path + '.tmp'
    [IO.File]::WriteAllText($temporary, $text + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Save-State {
    $script:RunState.updated_at = [DateTime]::UtcNow.ToString('o')
    Write-Utf8Json (Join-Path $script:RunDir 'run-state.json') $script:RunState
}

function Full-Path([string]$Path) {
    return [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path))
}

function Assert-External([string]$Path, [string]$Name) {
    $source = $WorkspaceRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if ($Path.Equals($WorkspaceRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($source, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name must be outside WorkspaceRoot; source and training artifacts stay separate."
    }
}

function Select-Python([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Python executable missing: $Path" }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Invoke-Stage([string]$Name, [string]$Python, [string[]]$Arguments) {
    $entry = [ordered]@{
        name = $Name; status = 'running'; started_at = [DateTime]::UtcNow.ToString('o')
        executable = $Python; arguments = @($Arguments); log = "$Name.log"; exit_code = $null
    }
    $script:RunState.stages += $entry
    Save-State
    Write-Output ("STAGE_START {0} run={1}" -f $Name, $script:RunDir)
    # Windows PowerShell 5.1 wraps native stderr as ErrorRecord. Preserve it in
    # the log without treating a warning as an exception before reading exit.
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 1 # A process-start error must not reuse an old zero.
        & $Python @Arguments 2>&1 | Tee-Object -FilePath (Join-Path $script:RunDir "$Name.log") | Out-Host
        $nativeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $oldPreference }
    $entry.exit_code = $nativeExit
    $entry.finished_at = [DateTime]::UtcNow.ToString('o')
    $entry.status = if ($nativeExit -eq 0) { 'passed' } else { 'failed' }
    Save-State
    if ($nativeExit -ne 0) {
        $failure = [Exception]::new("Stage $Name exited $nativeExit. Logs: $script:RunDir")
        $failure.Data['ExitCode'] = [int]$nativeExit
        $failure.Data['Stage'] = $Name
        throw $failure
    }
    Write-Output "STAGE_PASS $Name"
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This runner requires native Windows x64.' }
    $WorkspaceRoot = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
    if (-not (Test-Path -LiteralPath (Join-Path $WorkspaceRoot 'tools\decision_model\pipeline.py') -PathType Leaf)) {
        throw 'WorkspaceRoot must contain tools\decision_model\pipeline.py.'
    }
    $CacheRoot = Full-Path $CacheRoot
    if (-not $DataRoot) { $DataRoot = Join-Path $CacheRoot 'corpus' }
    if (-not $OutputRoot) { $OutputRoot = Join-Path $CacheRoot 'runs' }
    $DataRoot = Full-Path $DataRoot
    $OutputRoot = Full-Path $OutputRoot
    Assert-External $CacheRoot 'CacheRoot'
    Assert-External $DataRoot 'DataRoot'
    Assert-External $OutputRoot 'OutputRoot'
    if (-not $RunName) { $RunName = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8) }
    if ($RunName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$') { throw 'RunName must be a plain safe directory name.' }
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
    $script:RunDir = Join-Path $OutputRoot $RunName
    if (Test-Path -LiteralPath $script:RunDir) { throw 'Run already exists; choose a new RunName, also when resuming.' }
    New-Item -ItemType Directory -Path $script:RunDir | Out-Null
    $script:RunState = [ordered]@{
        schema = 'wxbot.windows-training.v1'; status = 'initializing'; exit_code = $null
        started_at = [DateTime]::UtcNow.ToString('o'); updated_at = $null
        workspace = $WorkspaceRoot; cache = $CacheRoot; data = $DataRoot; run = $script:RunDir
        device = $Device; epochs_this_run = $Epochs; batch = $Batch; learning_rate = $LearningRate
        seed = $Seed; resume = $Resume; stages = @(); source_sha256 = [ordered]@{}; input_sha256 = [ordered]@{}
        release_accepted = $false
    }
    Save-State
    Set-Location -LiteralPath $WorkspaceRoot
    foreach ($relative in @('tools/decision_model/pipeline.py', 'tools/decision_model/corpus.py',
        'tools/decision_model/benchmark.py', 'tools/decision_model/metrics.py',
        'src/core/local_decision.py', 'src/core/local_decision_worker.py', 'tools/decision_model/train-windows.ps1')) {
        $script:RunState.source_sha256[$relative] = (Get-FileHash -LiteralPath (Join-Path $WorkspaceRoot $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    if (-not $TrainPython) { $TrainPython = Join-Path $CacheRoot 'train-venv\Scripts\python.exe' }
    if (-not $RuntimePython) { $RuntimePython = Join-Path $CacheRoot 'runtime-venv\Scripts\python.exe' }
    $TrainPython = Select-Python $TrainPython
    $RuntimePython = Select-Python $RuntimePython
    if ($TrainPython.Equals($RuntimePython, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'TrainPython and RuntimePython must be separate virtual environments.'
    }
    if ($BaseDir) { $BaseDir = (Resolve-Path -LiteralPath $BaseDir).Path }
    if ($Resume) {
        $Resume = (Resolve-Path -LiteralPath $Resume).Path
        if (-not (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $Resume) 'training.json') -PathType Leaf)) {
            throw 'Resume requires task-head.safetensors with its original training.json beside it.'
        }
        $script:RunState.resume = $Resume
        $script:RunState.input_sha256['resume/task-head.safetensors'] = (Get-FileHash -LiteralPath $Resume -Algorithm SHA256).Hash.ToLowerInvariant()
        $script:RunState.input_sha256['resume/training.json'] = (Get-FileHash -LiteralPath (Join-Path (Split-Path -Parent $Resume) 'training.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    Save-State
    $trainProbe = @'
import importlib.metadata as md,json,os,platform,struct,sys
from pathlib import Path
assert os.name=='nt' and struct.calcsize('P')==8 and sys.version_info>=(3,10),'native Windows x64 Python 3.10+ required'
import torch,transformers,safetensors,scipy,onnx,onnxruntime,tokenizers,huggingface_hub
device=sys.argv[2]
available=torch.cuda.is_available()
if device=='cuda': assert available,'CUDA requested but torch.cuda.is_available() is false'
gpus=[]
if available:
 for i in range(torch.cuda.device_count()):
  p=torch.cuda.get_device_properties(i)
  gpus.append({'name':p.name,'compute_capability':list(torch.cuda.get_device_capability(i)),'total_memory_bytes':p.total_memory})
 if device!='cpu':
  x=torch.ones((2,2),device='cuda');assert float((x@x).sum().item())==8.;torch.cuda.synchronize()
result={'platform':platform.platform(),'python':sys.version,'executable':sys.executable,'cuda_available':available,'torch_cuda_version':torch.version.cuda,'gpus':gpus,'torch_cuda_arch_list':torch.cuda.get_arch_list() if available else [],'packages':{n:md.version(n) for n in ('torch','transformers','safetensors','scipy','onnx','onnxruntime','tokenizers','huggingface-hub','numpy')}}
Path(sys.argv[1]).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
'@
    $runtimeProbe = @'
import importlib.util,json,os,platform,struct,sys
from pathlib import Path
assert os.name=='nt' and struct.calcsize('P')==8 and sys.version_info>=(3,10),'native Windows x64 Python 3.10+ required'
assert importlib.util.find_spec('torch') is None,'RuntimePython must not contain training framework torch'
import onnxruntime as ort,numpy,tokenizers
assert 'CPUExecutionProvider' in ort.get_available_providers(),'CPU ONNX provider missing'
result={'platform':platform.platform(),'python':sys.version,'executable':sys.executable,'onnxruntime':ort.__version__,'numpy':numpy.__version__,'tokenizers':tokenizers.__version__,'available_providers':ort.get_available_providers(),'benchmark_provider':'CPUExecutionProvider','torch_present':False}
Path(sys.argv[1]).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
'@
    Invoke-Stage '00-training-environment' $TrainPython @('-c', $trainProbe, (Join-Path $script:RunDir 'training-environment.json'), $Device)
    Invoke-Stage '01-runtime-environment' $RuntimePython @('-c', $runtimeProbe, (Join-Path $script:RunDir 'runtime-environment.json'))
    if ($PrepareData) {
        if (Test-Path -LiteralPath $DataRoot) { throw 'PrepareData requires a new DataRoot; existing corpora are immutable inputs.' }
        Invoke-Stage '02-prepare' $TrainPython @('-m', 'tools.decision_model.pipeline', 'prepare', '--out', $DataRoot)
    }
    foreach ($name in @('train.jsonl', 'calibration.jsonl', 'test.jsonl', 'dataset.json')) {
        $path = Join-Path $DataRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Dataset input missing: $path" }
        $script:RunState.input_sha256[$name] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $script:RunState.status = 'running'
    Save-State
    $common = @('--cache', $CacheRoot, '--data', $DataRoot, '--out', $script:RunDir)
    if ($BaseDir) { $common += @('--base-dir', $BaseDir) }
    Invoke-Stage '03-fetch' $TrainPython (@('-m', 'tools.decision_model.pipeline', 'fetch') + $common)
    $trainArguments = @('-m', 'tools.decision_model.pipeline', 'train') + $common + @(
        '--device', $Device, '--epochs', [string]$Epochs, '--batch', [string]$Batch,
        '--lr', $LearningRate.ToString('R', [Globalization.CultureInfo]::InvariantCulture), '--seed', [string]$Seed)
    if ($Resume) { $trainArguments += @('--resume', $Resume) }
    Invoke-Stage '04-train' $TrainPython $trainArguments
    Invoke-Stage '05-calibrate' $TrainPython (@('-m', 'tools.decision_model.pipeline', 'calibrate') + $common)
    Invoke-Stage '06-export' $TrainPython (@('-m', 'tools.decision_model.pipeline', 'export') + $common)
    # Benchmark the no-Torch CPU runtime first: even a subsequent failed quality
    # gate retains genuine distribution-environment measurements. Evaluate also
    # uses CPU ONNX, but its project-level imports use the training environment.
    Invoke-Stage '07-benchmark-cpu' $RuntimePython @('-m', 'tools.decision_model.benchmark', '--model-dir', (Join-Path $script:RunDir 'package'), '--output', (Join-Path $script:RunDir 'benchmark-windows-cpu.json'))
    Invoke-Stage '08-evaluate' $TrainPython (@('-m', 'tools.decision_model.pipeline', 'evaluate') + @('--data', $DataRoot, '--out', $script:RunDir))
    Invoke-Stage '09-package-check' $RuntimePython @('-m', 'tools.decision_model.pipeline', 'package-check', '--out', (Join-Path $script:RunDir 'package'))
    $script:RunState.status = 'completed'
    $script:RunState.exit_code = 0
    # Passing this research run is necessary, not sufficient for a release:
    # blind conversational data/native installers remain separate acceptance.
    $script:RunState.release_accepted = $false
    Save-State
    $script:ExitCode = 0
    Write-Output "WINDOWS_TRAINING COMPLETE run=$script:RunDir release_accepted=false"
} catch {
    $failureCode = if ($_.Exception.Data.Contains('ExitCode')) { [int]$_.Exception.Data['ExitCode'] } else { 1 }
    $script:ExitCode = $failureCode
    if ($script:RunState) {
        $script:RunState.status = if ($_.Exception.Data['Stage'] -eq '08-evaluate' -and $failureCode -eq 2) { 'quality_failed' } else { 'failed' }
        $script:RunState.exit_code = $failureCode
        $script:RunState.error = $_.Exception.Message
        Save-State
    }
    [Console]::Error.WriteLine("WINDOWS_TRAINING FAIL exit=$failureCode " + $_.Exception.Message)
} finally { Set-Location -LiteralPath $OriginalLocation }
exit $script:ExitCode
