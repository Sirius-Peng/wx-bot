#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  微信 AI 助手 · macOS 一键启动
#  双击本文件即可运行（首次运行会自动安装依赖，约需 1-2 分钟）
# ═══════════════════════════════════════════════════════════════

cd "$(dirname "$0")" || exit 1

BOLD='\033[1m'; GREEN='\033[0;32m'; RED='\033[0;31m'
YELLOW='\033[0;33m'; DIM='\033[2m'; NC='\033[0m'

echo ""
echo -e "${BOLD}  微信 AI 助手${NC}"
echo -e "${DIM}  ────────────────────────────────────────${NC}"
echo ""

# ── 1. 找 Python ─────────────────────────────────────────────
PY=""
for c in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo -e "${RED}  ✗ 未找到 Python 3.9 或更高版本${NC}"
  echo ""
  echo "  请先安装 Python："
  echo "    1. 打开 https://www.python.org/downloads/"
  echo "    2. 下载并安装 Python 3.12（点击继续即可）"
  echo "    3. 安装完成后，重新双击本文件"
  echo ""
  read -r -p "  按回车键退出…" _
  exit 1
fi

echo -e "  ${GREEN}✓${NC} 找到 Python：$("$PY" --version 2>&1)"

# ── 2. 虚拟环境 ──────────────────────────────────────────────
VENV=".venv"
if [ ! -d "$VENV" ]; then
  echo -e "  ${DIM}首次运行，正在创建运行环境…${NC}"
  if ! "$PY" -m venv "$VENV" 2>/dev/null; then
    echo -e "${RED}  ✗ 创建运行环境失败${NC}"
    echo "  请确认已安装完整的 Python（不是 Xcode 命令行工具自带的版本）"
    read -r -p "  按回车键退出…" _
    exit 1
  fi
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate" || { echo -e "${RED}  ✗ 激活环境失败${NC}"; read -r -p "回车退出" _; exit 1; }

# ── 3. 安装依赖（仅首次）─────────────────────────────────────
STAMP="$VENV/.deps-installed"
if [ ! -f "$STAMP" ]; then
  echo -e "  ${DIM}正在安装依赖，请稍候（约 1-2 分钟）…${NC}"
  PIP_SOURCE="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  python -m pip install --upgrade pip -q --index-url "$PIP_SOURCE" 2>/dev/null || true
  if ! python -m pip install -q -r requirements.txt --index-url "$PIP_SOURCE"; then
    echo -e "${YELLOW}  国内镜像未完成，正在回退官方源重试…${NC}"
    if ! python -m pip install -q -r requirements.txt \
        --index-url https://pypi.org/simple; then
      echo -e "${RED}  ✗ 依赖安装失败${NC}"
      echo "  请检查网络连接后重试"
      read -r -p "  按回车键退出…" _
      exit 1
    fi
  fi
  touch "$STAMP"
  echo -e "  ${GREEN}✓${NC} 依赖安装完成"
fi

# ── 4. 启动 ──────────────────────────────────────────────────
echo ""
echo -e "${DIM}  ────────────────────────────────────────${NC}"
echo -e "  ${GREEN}正在启动服务…${NC}"
echo -e "  浏览器将自动打开管理界面"
echo ""
echo -e "  ${YELLOW}关闭本窗口即可停止机器人${NC}"
echo -e "${DIM}  ────────────────────────────────────────${NC}"
echo ""

python -m src.server

echo ""
echo -e "${YELLOW}  服务已停止${NC}"
read -r -p "  按回车键关闭窗口…" _
