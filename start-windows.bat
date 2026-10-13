@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   微信 AI 助手
echo   ----------------------------------------
echo.

REM ── 1. 找 Python ─────────────────────────────────────────────
set "PY="
for %%C in (python py python3) do (
  if not defined PY (
    %%C -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PY=%%C"
  )
)

if not defined PY (
  echo   [X] 未找到 Python 3.9 或更高版本
  echo.
  echo   请先安装 Python：
  echo     1. 打开 https://www.python.org/downloads/
  echo     2. 下载并安装 Python 3.12
  echo     3. 安装时务必勾选 "Add Python to PATH"
  echo     4. 安装完成后重新双击本文件
  echo.
  pause
  exit /b 1
)

for /f "tokens=*" %%V in ('%PY% --version 2^>^&1') do echo   [OK] 找到 %%V

REM ── 2. 虚拟环境 ──────────────────────────────────────────────
if not exist ".venv" (
  echo   首次运行，正在创建运行环境...
  %PY% -m venv .venv
  if !errorlevel! neq 0 (
    echo   [X] 创建运行环境失败
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

REM ── 3. 安装依赖（仅首次）─────────────────────────────────────
if not exist ".venv\.deps-installed" (
  echo   正在安装依赖，请稍候（约 1-2 分钟）...
  if not defined PIP_INDEX_URL set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
  python -m pip install --upgrade pip -q --index-url "!PIP_INDEX_URL!" >nul 2>&1
  python -m pip install -q -r requirements.txt --index-url "!PIP_INDEX_URL!"
  if !errorlevel! neq 0 (
    echo   国内镜像未完成，正在回退官方源重试...
    python -m pip install -q -r requirements.txt --index-url https://pypi.org/simple
    if !errorlevel! neq 0 (
      echo   [X] 依赖安装失败，请检查网络后重试
      pause
      exit /b 1
    )
  )
  echo. > ".venv\.deps-installed"
  echo   [OK] 依赖安装完成
)

REM ── 4. 启动 ──────────────────────────────────────────────────
echo.
echo   ----------------------------------------
echo   正在启动服务，浏览器将自动打开管理界面
echo.
echo   关闭本窗口即可停止机器人
echo   ----------------------------------------
echo.

python -m src.server

echo.
echo   服务已停止
pause
