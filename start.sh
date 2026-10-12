#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  微信 AI 助手 · Linux / macOS 命令行启动
#  用法： ./start.sh
# ═══════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

BOLD='\033[1m'; GREEN='\033[0;32m'; RED='\033[0;31m'
YELLOW='\033[0;33m'; DIM='\033[2m'; NC='\033[0m'

echo ""
echo -e "${BOLD}  微信 AI 助手${NC}"
echo -e "${DIM}  ────────────────────────────────────────${NC}"
echo ""

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done

if [ -z "$PY" ]; then
  echo -e "${RED}  ✗ 未找到 Python 3.9+${NC}"
  echo "  Ubuntu/Debian:  sudo apt install python3 python3-venv"
  echo "  CentOS/RHEL:    sudo yum install python3"
  exit 1
fi
echo -e "  ${GREEN}✓${NC} $($PY --version 2>&1)"

VENV=".venv"
[ -d "$VENV" ] || { echo -e "  ${DIM}创建运行环境…${NC}"; "$PY" -m venv "$VENV"; }
# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ ! -f "$VENV/.deps-installed" ]; then
  echo -e "  ${DIM}安装依赖…${NC}"
  python -m pip install --upgrade pip -q
  python -m pip install -q -r requirements.txt \
    || python -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  touch "$VENV/.deps-installed"
  echo -e "  ${GREEN}✓${NC} 依赖安装完成"
fi

echo ""
echo -e "  ${GREEN}启动中…${NC}  管理界面： ${BOLD}http://127.0.0.1:8000${NC}"
echo -e "  ${YELLOW}按 Ctrl+C 停止${NC}"
echo ""

exec python -m src.server
