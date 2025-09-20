#!/usr/bin/env bash
set -euo pipefail

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 子命令：start(默认)/stop/restart/dev
CMD="${1:-start}"
UVICORN_ARGS="--host 127.0.0.1 --port 8000"
if [ "$CMD" = "dev" ]; then
  UVICORN_ARGS="$UVICORN_ARGS --reload"
fi

stop_server() {
  if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    PIDS=$(lsof -t -nP -iTCP:8000 -sTCP:LISTEN || true)
    if [ -n "${PIDS:-}" ]; then
      echo "停止进程: $PIDS"
      kill -TERM $PIDS || true
      sleep 1
      if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
        echo "强制结束残留进程"
        kill -KILL $PIDS || true
      fi
    fi
  else
    echo "端口 8000 未被占用"
  fi
}

echo "[1/6] 初始化 Python 环境 (pyenv 可选)"
if command -v pyenv >/dev/null 2>&1; then
  export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
  export PATH="$PYENV_ROOT/bin:$PATH"
  eval "$(pyenv init -)"
  # 仅在当前目录未指定 Python 版本时配置 3.12.5
  if [ ! -f ".python-version" ]; then
    pyenv install -s 3.12.5
    pyenv local 3.12.5
  fi
fi

PYTHON_BIN=${PYTHON_BIN:-python3}
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "错误: 未找到 Python，可通过 pyenv 安装 3.12.5 或设置 PYTHON_BIN 环境变量。" >&2
  exit 1
fi

echo "[2/6] 创建并激活虚拟环境 .venv"
if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -V

echo "[3/6] 升级基础工具并按需安装依赖"
pip install -U pip setuptools wheel

# 依赖安装策略：PIP_MODE=auto|always|skip（默认 auto）
PIP_MODE="${PIP_MODE:-auto}"

hash_cmd() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v md5 >/dev/null 2>&1; then
    md5 -q "$1"
  else
    # 兜底：无可用哈希命令时，总是触发安装
    echo "NOHASH"
  fi
}

NEED_INSTALL=0
if [ "$PIP_MODE" = "always" ]; then
  NEED_INSTALL=1
elif [ "$PIP_MODE" = "skip" ]; then
  NEED_INSTALL=0
else
  if [ ! -f ".venv/requirements.sha256" ]; then
    NEED_INSTALL=1
  else
    CURR_HASH=$(hash_cmd requirements.txt)
    OLD_HASH=$(cat .venv/requirements.sha256 2>/dev/null || true)
    if [ "$CURR_HASH" != "$OLD_HASH" ]; then
      NEED_INSTALL=1
    fi
  fi
fi

if [ $NEED_INSTALL -eq 1 ]; then
  echo "安装/更新依赖... (PIP_MODE=$PIP_MODE)"
  pip install -r requirements.txt
  # 确保满足 SQLAlchemy 导入 greenlet 需求（一次性安装）
  pip install 'greenlet==3.0.3'
  CURR_HASH=$(hash_cmd requirements.txt)
  echo "$CURR_HASH" > .venv/requirements.sha256
else
  echo "跳过依赖安装 (PIP_MODE=$PIP_MODE)"
fi



echo "[5/6] 检查端口占用 (127.0.0.1:8000)"
if [ "$CMD" = "stop" ]; then
  stop_server
  exit 0
fi

if [ "$CMD" = "restart" ]; then
  stop_server
fi

if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "提示: 端口 8000 已被占用，请关闭后重试或执行 \"$0 stop\"。"
  exit 1
fi

echo "[6/6] 启动服务: http://127.0.0.1:8000 (控制台 /control) 模式: $CMD"
exec python -m uvicorn app:app $UVICORN_ARGS


