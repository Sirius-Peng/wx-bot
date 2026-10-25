#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  把 Python 引擎打包成单文件可执行程序
#
#  用法：  bash scripts/build-backend.sh
#  产出：  resources/backend/wxbot-server
#
#  Electron 会把该文件作为 extraResources 打进 App，
#  运行时不依赖用户机器上的 Python 环境。
# ═══════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")/.."

DESKTOP_DIR="$(pwd)"
REPO_ROOT="$(cd .. && pwd)"
OUT_DIR="$DESKTOP_DIR/resources/backend"

GREEN='\033[0;32m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'

echo ""
echo "  打包 Python 后端"
echo "  ────────────────────────────────────────"

# ── 1. 选 Python ─────────────────────────────────────────────
PY=""
for c in "$REPO_ROOT/.venv/bin/python3" python3 python; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -z "$PY" ] && { echo -e "${RED}  ✗ 未找到 Python 3.9+${NC}"; exit 1; }
echo -e "  ${DIM}Python: $($PY --version 2>&1)${NC}"

# ── 2. 安装打包依赖 ──────────────────────────────────────────
echo -e "  ${DIM}检查打包依赖…${NC}"
PIP_SOURCE="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
"$PY" -m pip install -q -r "$REPO_ROOT/requirements.txt" pyinstaller --index-url "$PIP_SOURCE" || \
  "$PY" -m pip install -q -r "$REPO_ROOT/requirements.txt" pyinstaller --index-url https://pypi.org/simple

"$PY" "$DESKTOP_DIR/scripts/collect-notices.py"

# ── 3. 清理旧产物 ────────────────────────────────────────────
# 只清后端自己的产物；dist/ 是应用打包的输出，不归这里管
rm -rf "$DESKTOP_DIR/build" "$OUT_DIR"
mkdir -p "$OUT_DIR"

# PyInstaller 默认把缓存放在 ~/Library，某些受限环境会拦截其清理动作。
# 指到项目内部，既避免权限问题，也让缓存随项目一起可清理。
export PYINSTALLER_CONFIG_DIR="$DESKTOP_DIR/.pyinstaller-cache"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

# ── 4. 打包 ──────────────────────────────────────────────────
echo -e "  ${DIM}正在打包（首次约 1-2 分钟）…${NC}"

SEP=":"   # macOS / Linux 用冒号分隔 add-data
"$PY" -m PyInstaller \
  --onefile \
  --noconfirm \
  --clean \
  --name wxbot-server \
  --distpath "$OUT_DIR" \
  --workpath "$DESKTOP_DIR/build" \
  --specpath "$DESKTOP_DIR/build" \
  --paths "$REPO_ROOT" \
  --add-data "$REPO_ROOT/src/web${SEP}web" \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import uvicorn.lifespan.off \
  --collect-all uvicorn \
  --collect-all fastapi \
  --collect-all starlette \
  --collect-all pydantic \
  --collect-all anyio \
  --collect-all httpx \
  --collect-all httpcore \
  --collect-all certifi \
  --collect-all tzdata \
  --exclude-module src.core.local_decision \
  --exclude-module src.core.local_decision_worker \
  --exclude-module src.core.local_decision_cpu \
  --exclude-module onnxruntime \
  --exclude-module onnx \
  --exclude-module tokenizers \
  --exclude-module numpy \
  --exclude-module torch \
  --exclude-module transformers \
  --collect-all jieba \
  "$DESKTOP_DIR/backend/entry.py"

# ── 5. 校验 ──────────────────────────────────────────────────
BIN="$OUT_DIR/wxbot-server"
if [ ! -f "$BIN" ]; then
  echo -e "${RED}  ✗ 打包失败：未生成 $BIN${NC}"
  exit 1
fi
chmod +x "$BIN"

SIZE=$(du -h "$BIN" | cut -f1)
echo -e "  ${GREEN}✓${NC} 后端打包完成：$BIN（$SIZE）"

# ── 6. 冒烟测试：真的能起来吗 ────────────────────────────────
echo -e "  ${DIM}冒烟测试（启动后端并探测健康检查）…${NC}"
TEST_PORT=8799
WXBOT_PORT=$TEST_PORT WXBOT_NO_BROWSER=1 WXBOT_DATA_DIR=/tmp/wxbot-smoke \
  "$BIN" > /tmp/wxbot-smoke.log 2>&1 &
PID=$!

OK=0
for _ in $(seq 1 40); do
  sleep 0.5
  if curl -fsS "http://127.0.0.1:$TEST_PORT/healthz" >/dev/null 2>&1; then OK=1; break; fi
  kill -0 $PID 2>/dev/null || break
done

kill $PID 2>/dev/null || true
wait $PID 2>/dev/null || true
rm -rf /tmp/wxbot-smoke

if [ "$OK" = "1" ]; then
  echo -e "  ${GREEN}✓${NC} 冒烟测试通过，后端可正常启动"
else
  echo -e "${RED}  ✗ 冒烟测试失败，日志：${NC}"
  tail -20 /tmp/wxbot-smoke.log 2>/dev/null
  exit 1
fi

echo ""
echo -e "  ${GREEN}完成${NC} —— 现在可以执行 npm run pack:mac 打包桌面应用"
echo ""
