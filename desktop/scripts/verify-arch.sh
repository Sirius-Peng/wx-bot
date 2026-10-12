#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  打包后架构校验
#
#  用法：  bash scripts/verify-arch.sh [期望架构]
#          期望架构：arm64（默认）| x64 | universal
#
#  为什么需要这个脚本：
#    曾因 electron-builder.yml 里写了 arch: [arm64, x64]，命令行的 --arm64
#    未能覆盖，导致同时构建两个架构、最终 DMG 落到 x64 那份上。
#    用户装到 Apple Silicon 机器上被 macOS 提示"此应用为 Intel 芯片设计"。
#    这类问题在打包日志里完全看不出来，必须主动校验二进制。
# ═══════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")/.."

EXPECT="${1:-arm64}"
# x64 在 Mach-O 里的名字是 x86_64
case "$EXPECT" in
  x64) MATCH="x86_64" ;;
  *)   MATCH="$EXPECT" ;;
esac

RED='\033[0;31m'; GREEN='\033[0;32m'; DIM='\033[2m'; NC='\033[0m'

echo ""
echo "  架构校验（期望：$EXPECT）"
echo "  ────────────────────────────────────────"

# 找到最新构建出的 .app
APP=$(find dist -maxdepth 2 -name "*.app" -type d 2>/dev/null | head -1)
if [ -z "$APP" ]; then
  echo -e "${RED}  ✗ dist/ 下没有找到 .app，请先执行打包${NC}"
  exit 1
fi
echo -e "  ${DIM}应用：$APP${NC}"

FAIL=0

check() {
  local label="$1" path="$2"
  if [ ! -e "$path" ]; then
    echo -e "  ${RED}✗${NC} $label —— 文件不存在"
    FAIL=1
    return
  fi
  local archs
  archs=$(lipo -archs "$path" 2>/dev/null || echo "unknown")
  if echo "$archs" | grep -q "$MATCH"; then
    # universal 情况下允许多个架构
    if [ "$EXPECT" != "universal" ] && [ "$(echo "$archs" | wc -w)" -gt 1 ]; then
      echo -e "  ${RED}✗${NC} $label —— 含多个架构（$archs），期望单一 $EXPECT"
      FAIL=1
    else
      echo -e "  ${GREEN}✓${NC} $label —— $archs"
    fi
  else
    echo -e "  ${RED}✗${NC} $label —— 实际 $archs，期望 $EXPECT"
    FAIL=1
  fi
}

# ── 逐个关键二进制校验 ───────────────────────────────────────
check "主可执行文件      " "$APP/Contents/MacOS/"*
check "Electron Framework" "$APP/Contents/Frameworks/Electron Framework.framework/Electron Framework"
check "Python 后端 sidecar" "$APP/Contents/Resources/backend/wxbot-server"*

# 顺带检查 Info.plist 里的最低系统版本
MINVER=$(/usr/libexec/PlistBuddy -c "Print :LSMinimumSystemVersion" "$APP/Contents/Info.plist" 2>/dev/null || echo "?")
echo -e "  ${DIM}最低系统版本：$MINVER${NC}"

echo ""
if [ "$FAIL" = "0" ]; then
  echo -e "  ${GREEN}架构校验通过${NC} —— 可以分发"
  echo ""
else
  echo -e "  ${RED}架构校验失败${NC} —— 不要分发这个包！"
  echo "  常见原因：electron-builder.yml 的 mac.target.arch 写了多个架构，"
  echo "            导致产出的包不是期望架构。"
  echo ""
  exit 1
fi
