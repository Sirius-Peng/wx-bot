#Requires -Version 5.1
param([string]$Python = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$DesktopDir = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent $DesktopDir
$OutDir = Join-Path $DesktopDir 'resources\backend'
$BuildDir = Join-Path $DesktopDir 'build\windows'
Set-Location -LiteralPath $DesktopDir
$candidates = @($Python, (Join-Path $RepoRoot '.venv\Scripts\python.exe'), 'python', 'py', 'python3')
$selected = $null
foreach ($candidate in $candidates) {
 if (-not $candidate) { continue }
 try {
  $command = Get-Command $candidate -ErrorAction Stop
  & $command.Source -c 'import sys,struct; sys.exit(0 if sys.version_info >= (3,10) and struct.calcsize(chr(80)) == 8 else 1)' 2>$null
  if ($LASTEXITCODE -eq 0) { $selected = $command.Source; break }
 } catch { }
}
if (-not $selected) { throw 'A native Windows x64 Python 3.10+ interpreter is required.' }
& $selected --version
$PipSource = if ($env:PIP_INDEX_URL) { $env:PIP_INDEX_URL } else { 'https://pypi.tuna.tsinghua.edu.cn/simple' }
& $selected -m pip install -r (Join-Path $RepoRoot 'requirements.txt') pyinstaller --index-url $PipSource
if ($LASTEXITCODE -ne 0) {
 Write-Output 'Mainland mirror failed; retrying official PyPI.'
 & $selected -m pip install -r (Join-Path $RepoRoot 'requirements.txt') pyinstaller --index-url 'https://pypi.org/simple'
}
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed on both sources.' }
if (Test-Path -LiteralPath $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
if (Test-Path -LiteralPath $BuildDir) { Remove-Item -LiteralPath $BuildDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $OutDir,$BuildDir | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path $DesktopDir '.pyinstaller-cache\windows'
$arguments = @('-m','PyInstaller','--onedir','--noconfirm','--clean',
 '--name','wxbot-server','--distpath',$OutDir,'--workpath',$BuildDir,'--specpath',$BuildDir,
 '--paths',$RepoRoot,'--add-data',((Join-Path $RepoRoot 'src\web') + ';web'))
foreach ($module in @('uvicorn.logging','uvicorn.loops.auto','uvicorn.loops.asyncio',
 'uvicorn.protocols.http.auto','uvicorn.protocols.http.h11_impl',
 'uvicorn.protocols.websockets.auto','uvicorn.lifespan.on','uvicorn.lifespan.off')) {
 $arguments += @('--hidden-import',$module)
}
foreach ($module in @('uvicorn','fastapi','starlette','pydantic','anyio','httpx','httpcore','certifi','tzdata','jieba')) {
 $arguments += @('--collect-all',$module)
}
$arguments += (Join-Path $DesktopDir 'backend\entry.py')
& $selected @arguments
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
$exe = Join-Path $OutDir 'wxbot-server\wxbot-server.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Native backend executable is missing.' }
& node (Join-Path $PSScriptRoot 'verify-windows.cjs') --backend-only
if ($LASTEXITCODE -ne 0) { throw 'Native backend PE validation failed.' }
Write-Output 'WINDOWS_BACKEND PASS: native=x64; layout=onedir; executable=wxbot-server/wxbot-server.exe'
