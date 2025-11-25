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
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# ============================================================
# 加载 .env 配置
# ============================================================

if [ -f ".env" ]; then
    # 读取 .env 文件，忽略注释和空行
    while IFS='=' read -r key value; do
        # 跳过注释和空行
        [[ "$key" =~ ^#.*$ ]] && continue
        [[ -z "$key" ]] && continue
        # 去除值两端的引号
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        # 导出变量
        export "$key=$value"
    done < .env
fi

# 配置（优先使用环境变量/命令行，否则使用默认值）
CMD="${1:-start}"
HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-8000}"
DB_PORT="${DB_PORT:-5432}"
SSH_HOST="${SSH_TUNNEL_HOST:-}"
SSH_PID_FILE=".venv/.ssh_tunnel.pid"

# ============================================================
# 清理函数
# ============================================================

cleanup() {
    echo ""
    info "正在关闭..."
    
    # 关闭 SSH 隧道
    if [ -f "$SSH_PID_FILE" ]; then
        SSH_PID=$(cat "$SSH_PID_FILE" 2>/dev/null || echo "")
        if [ -n "$SSH_PID" ] && kill -0 "$SSH_PID" 2>/dev/null; then
            kill "$SSH_PID" 2>/dev/null || true
            success "SSH 隧道已关闭"
        fi
        rm -f "$SSH_PID_FILE"
    fi
    
    # 关闭 uvicorn
    if [ -n "${UVICORN_PID:-}" ] && kill -0 "$UVICORN_PID" 2>/dev/null; then
        kill "$UVICORN_PID" 2>/dev/null || true
        wait "$UVICORN_PID" 2>/dev/null || true
    fi
    
    success "已退出"
    exit 0
}

# 捕获信号
trap cleanup SIGINT SIGTERM EXIT

# ============================================================
# 进程管理
# ============================================================

stop_server() {
    # 关闭 uvicorn
    if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
        PIDS=$(lsof -t -nP -iTCP:${PORT} -sTCP:LISTEN || true)
        if [ -n "${PIDS:-}" ]; then
            info "停止 Web 服务: $PIDS"
            kill -TERM $PIDS 2>/dev/null || true
            sleep 1
            if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
                kill -KILL $PIDS 2>/dev/null || true
            fi
        fi
    fi
    
    # 关闭 SSH 隧道
    if [ -f "$SSH_PID_FILE" ]; then
        SSH_PID=$(cat "$SSH_PID_FILE" 2>/dev/null || echo "")
        if [ -n "$SSH_PID" ] && kill -0 "$SSH_PID" 2>/dev/null; then
            info "停止 SSH 隧道: $SSH_PID"
            kill "$SSH_PID" 2>/dev/null || true
        fi
        rm -f "$SSH_PID_FILE"
    fi
    
    # 清理所有可能残留的 SSH 隧道
    pkill -f "ssh.*-L.*${DB_PORT}:localhost:${DB_PORT}.*${SSH_HOST}" 2>/dev/null || true
    
    success "服务已停止"
}

stop_ssh_tunnel() {
    if [ -f "$SSH_PID_FILE" ]; then
        SSH_PID=$(cat "$SSH_PID_FILE" 2>/dev/null || echo "")
        if [ -n "$SSH_PID" ] && kill -0 "$SSH_PID" 2>/dev/null; then
            kill "$SSH_PID" 2>/dev/null || true
        fi
        rm -f "$SSH_PID_FILE"
    fi
    pkill -f "ssh.*-L.*${DB_PORT}:localhost:${DB_PORT}.*${SSH_HOST}" 2>/dev/null || true
}

if [ "$CMD" = "stop" ]; then
    stop_server
    exit 0
fi

if [ "$CMD" = "restart" ]; then
    stop_server
    sleep 1
fi

# ============================================================
# 启动流程
# ============================================================

echo ""
echo "╔════════════════════════════════════════╗"
echo "║     群聊弹幕系统 - 启动脚本            ║"
echo "╚════════════════════════════════════════╝"
echo ""

# [1/5] Python 环境
info "[1/5] 检查 Python 环境..."

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    error "未找到 Python，请安装 Python 3.9+"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
success "Python $PYTHON_VERSION"

# [2/5] 虚拟环境
info "[2/5] 配置虚拟环境..."

if [ ! -d ".venv" ] || [ ! -f ".venv/bin/python" ]; then
    info "创建虚拟环境 .venv"
    rm -rf .venv 2>/dev/null || true
    "$PYTHON_BIN" -m venv .venv
fi

VENV_PYTHON=".venv/bin/python"
VENV_PIP=".venv/bin/pip"

if [ ! -f "$VENV_PYTHON" ]; then
    error "虚拟环境创建失败"
    exit 1
fi

success "虚拟环境已就绪"

# [3/5] 依赖安装
info "[3/5] 检查依赖..."

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
    HASH_FILE=".venv/.requirements.hash"
    CURR_HASH=$(hash_file requirements.txt)
    OLD_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")
    
    if [ "$CURR_HASH" != "$OLD_HASH" ]; then
        NEED_INSTALL=1
    fi
fi

if [ $NEED_INSTALL -eq 1 ]; then
    info "安装依赖..."
    "$VENV_PIP" install -q -U pip setuptools wheel
    "$VENV_PIP" install -q -r requirements.txt
    hash_file requirements.txt > ".venv/.requirements.hash"
    success "依赖已安装"
else
    success "依赖已是最新"
fi

# [4/5] SSH 隧道
info "[4/5] 建立数据库连接..."

# 先清理可能存在的旧隧道
stop_ssh_tunnel

# 检查本地端口是否已被占用
if lsof -nP -iTCP:${DB_PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    warn "端口 ${DB_PORT} 已被占用，假设数据库已可用"
elif [ -z "$SSH_HOST" ]; then
    # 未配置 SSH 隧道，假设数据库在本地或已有其他方式连接
    info "未配置 SSH_TUNNEL_HOST，跳过隧道建立"
else
    info "建立 SSH 隧道到 ${SSH_HOST}..."
    
    # 启动 SSH 隧道（后台运行，自动重连）
    ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -L ${DB_PORT}:localhost:${DB_PORT} ${SSH_HOST}
    
    # 等待隧道建立
    sleep 2
    
    # 检查隧道是否成功
    if lsof -nP -iTCP:${DB_PORT} -sTCP:LISTEN >/dev/null 2>&1; then
        # 保存 SSH 进程 PID
        SSH_PID=$(pgrep -f "ssh.*-L.*${DB_PORT}:localhost:${DB_PORT}.*${SSH_HOST}" | head -1)
        if [ -n "$SSH_PID" ]; then
            echo "$SSH_PID" > "$SSH_PID_FILE"
        fi
        success "SSH 隧道已建立 (PID: ${SSH_PID:-unknown})"
    else
        error "SSH 隧道建立失败"
        warn "请检查网络连接和 SSH 配置"
        exit 1
    fi
fi

# [5/5] 端口检查
info "[5/5] 检查 Web 端口 ${HOST}:${PORT}..."

if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    error "端口 ${PORT} 已被占用"
    warn "请执行 \"$0 stop\" 停止现有服务"
    exit 1
fi
success "端口可用"

# ============================================================
# 启动服务
# ============================================================

echo ""
echo "════════════════════════════════════════"
echo ""

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
warn "按 Ctrl+C 停止服务"
echo ""
echo "════════════════════════════════════════"
echo ""

# 启动 uvicorn（不使用 exec，以便信号处理正常工作）
"$VENV_PYTHON" -m uvicorn app:app $UVICORN_ARGS &
UVICORN_PID=$!

# 等待 uvicorn 进程
wait $UVICORN_PID
