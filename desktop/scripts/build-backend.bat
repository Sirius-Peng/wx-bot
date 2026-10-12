@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0.."

REM ═══════════════════════════════════════════════════════════════
REM  把 Python 引擎打包成单文件可执行程序（Windows）
REM  用法：  scripts\build-backend.bat
REM  产出：  resources\backend\wxbot-server.exe
REM ═══════════════════════════════════════════════════════════════

set "DESKTOP_DIR=%cd%"
for %%I in ("%cd%\..") do set "REPO_ROOT=%%~fI"
set "OUT_DIR=%DESKTOP_DIR%\resources\backend"

echo.
echo   打包 Python 后端（Windows）
echo   ----------------------------------------

REM ── 1. 选 Python ─────────────────────────────────────────────
set "PY="
for %%C in ("%REPO_ROOT%\.venv\Scripts\python.exe" python py python3) do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PY=%%~C"
  )
)
if not defined PY (
  echo   [X] 未找到 Python 3.9+
  pause
  exit /b 1
)
for /f "tokens=*" %%V in ('%PY% --version 2^>^&1') do echo   Python: %%V

REM ── 2. 安装打包依赖 ──────────────────────────────────────────
echo   检查打包依赖...
%PY% -m pip install -q -r "%REPO_ROOT%\requirements.txt" >nul 2>&1
%PY% -m pip install -q pyinstaller
if !errorlevel! neq 0 (
  %PY% -m pip install -q pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
)

REM ── 3. 清理旧产物 ────────────────────────────────────────────
if exist "%DESKTOP_DIR%\build" rmdir /s /q "%DESKTOP_DIR%\build"
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"
mkdir "%OUT_DIR%" 2>nul

REM ── 4. 打包 ──────────────────────────────────────────────────
echo   正在打包（首次约 1-2 分钟）...

%PY% -m PyInstaller ^
  --onefile ^
  --noconfirm ^
  --clean ^
  --name wxbot-server ^
  --distpath "%OUT_DIR%" ^
  --workpath "%DESKTOP_DIR%\build" ^
  --specpath "%DESKTOP_DIR%\build" ^
  --paths "%REPO_ROOT%" ^
  --add-data "%REPO_ROOT%\src\web;web" ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.loops.asyncio ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.http.h11_impl ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on ^
  --hidden-import uvicorn.lifespan.off ^
  --collect-all uvicorn ^
  --collect-all fastapi ^
  --collect-all starlette ^
  --collect-all pydantic ^
  --collect-all anyio ^
  --collect-all httpx ^
  --collect-all httpcore ^
  --collect-all certifi ^
  "%DESKTOP_DIR%\backend\entry.py"

REM ── 5. 校验 ──────────────────────────────────────────────────
if not exist "%OUT_DIR%\wxbot-server.exe" (
  echo   [X] 打包失败：未生成 wxbot-server.exe
  pause
  exit /b 1
)
echo   [OK] 后端打包完成：%OUT_DIR%\wxbot-server.exe

echo.
echo   完成 —— 现在可以执行 npm run pack:win 打包桌面应用
echo.
pause
