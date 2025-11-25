#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# 群聊弹幕系统启动脚本
# 用法: ./start.sh [start|stop|restart|dev]
# ============================================================

cd "$(dirname "$0")"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# 命令参数
CMD="${1:-start}"
HOST="127.0.0.1"
PORT="8000"

# ============================================================
# 进程管理函数
# ============================================================

stop_server() {
    if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
        PIDS=$(lsof -t -nP -iTCP:${PORT} -sTCP:LISTEN || true)
        if [ -n "${PIDS:-}" ]; then
            info "停止进程: $PIDS"
            kill -TERM $PIDS 2>/dev/null || true
            sleep 1
            # 检查是否还在运行
            if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
                warn "强制结束残留进程"
                kill -KILL $PIDS 2>/dev/null || true
            fi
            success "服务已停止"
        fi
    else
        info "端口 ${PORT} 未被占用"
    fi
}

# 如果是 stop 命令，直接执行并退出
if [ "$CMD" = "stop" ]; then
    stop_server
    exit 0
fi

# 如果是 restart 命令，先停止
if [ "$CMD" = "restart" ]; then
    stop_server
    sleep 1
fi

# ============================================================
# 环境检查
# ============================================================

echo ""
echo "╔════════════════════════════════════════╗"
echo "║     群聊弹幕系统 - 启动脚本            ║"
echo "╚════════════════════════════════════════╝"
echo ""

# [1/4] Python 环境
info "[1/4] 检查 Python 环境..."

# 优先使用 pyenv
if command -v pyenv >/dev/null 2>&1; then
    export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
    export PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init -)" 2>/dev/null || true
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    error "未找到 Python，请安装 Python 3.9+ 或设置 PYTHON_BIN 环境变量"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
success "Python $PYTHON_VERSION"

# [2/4] 虚拟环境
info "[2/4] 配置虚拟环境..."

if [ ! -d ".venv" ]; then
    info "创建虚拟环境 .venv"
    "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
success "虚拟环境已激活"

# [3/4] 依赖安装
info "[3/4] 检查依赖..."

# 计算 requirements.txt 的哈希值
hash_file() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
    elif command -v md5sum >/dev/null 2>&1; then
        md5sum "$1" 2>/dev/null | awk '{print $1}'
    elif command -v md5 >/dev/null 2>&1; then
        md5 -q "$1" 2>/dev/null
    else
        echo "NOHASH"
    fi
}

PIP_MODE="${PIP_MODE:-auto}"
NEED_INSTALL=0

if [ "$PIP_MODE" = "always" ]; then
    NEED_INSTALL=1
elif [ "$PIP_MODE" = "skip" ]; then
    NEED_INSTALL=0
else
    # auto 模式：检查哈希值
    HASH_FILE=".venv/.requirements.hash"
    CURR_HASH=$(hash_file requirements.txt)
    OLD_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")
    
    if [ "$CURR_HASH" != "$OLD_HASH" ]; then
        NEED_INSTALL=1
    fi
fi

if [ $NEED_INSTALL -eq 1 ]; then
    info "安装依赖..."
    pip install -q -U pip setuptools wheel
    pip install -q -r requirements.txt
    
    # 保存哈希值
    hash_file requirements.txt > ".venv/.requirements.hash"
    success "依赖已安装"
else
    success "依赖已是最新"
fi

# [4/4] 端口检查
info "[4/4] 检查端口 ${HOST}:${PORT}..."

if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    error "端口 ${PORT} 已被占用"
    warn "请执行 \"$0 stop\" 停止现有服务，或手动关闭占用进程"
    exit 1
fi
success "端口可用"

# ============================================================
# 启动服务
# ============================================================

echo ""
echo "════════════════════════════════════════"
echo ""

# 构建 uvicorn 参数
UVICORN_ARGS="--host ${HOST} --port ${PORT}"

if [ "$CMD" = "dev" ]; then
    UVICORN_ARGS="$UVICORN_ARGS --reload"
    info "开发模式 (热重载已启用)"
else
    info "生产模式"
fi

echo ""
success "弹幕页面: http://${HOST}:${PORT}"
success "控制面板: http://${HOST}:${PORT}/control"
echo ""
echo "════════════════════════════════════════"
echo ""

# 启动服务
exec python -m uvicorn app:app $UVICORN_ARGS
