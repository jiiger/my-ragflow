#!/usr/bin/env bash
# my-ragflow 学习版一键启动脚本(dev 模式)
# 拉起三个进程: 后端主服务(9380) + task_executor(解析消费端) + 前端 vite dev(9222)
# 用法: bash launch_dev.sh [--no-web]
# 停止: bash stop_dev.sh   或  kill $(cat logs/*.pid)
set -uo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

# 环境变量(国内网络必需, 见 run-service-workflow.md)
export LITELLM_LOCAL_MODEL_COST_MAP=true

echo "==================== my-ragflow dev 启动 ===================="
echo "工作目录: $ROOT"

# ---------- 0) 检查 docker 四件套 ----------
echo ""
echo "[0/4] 检查基础设施(Docker 四件套)..."
if command -v docker >/dev/null 2>&1; then
  REQUIRED=(docker-mysql-1 docker-minio-1 docker-es01-1 docker-redis-1)
  RUNNING=$(docker ps --format '{{.Names}}' 2>/dev/null)
  for c in "${REQUIRED[@]}"; do
    if ! echo "$RUNNING" | grep -qx "$c"; then
      echo "  ⚠️  缺少容器 $c —— 请先启动: "
      echo "      cd /home/yang/code/ragflow/docker && MEM_LIMIT=2147483648 docker compose --profile elasticsearch up -d mysql redis minio es01"
      exit 1
    fi
  done
  echo "  ✓ 四件套均在运行"
else
  echo "  ⚠️  docker 命令不可用, 跳过检查(请确保 mysql/minio/es/redis 已起)"
fi

# ---------- 1) 后端主服务 ----------
start() { # start <name> <cmd...>
  local name="$1"; shift
  local pidfile="$LOG_DIR/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "  ⚠️  $name 已在运行 (pid $(cat "$pidfile")), 跳过"
    return 0
  fi
  echo "  启动 $name ..."
  # setsid: 进程自成新进程组(PGID = $!), stop 时 kill -- -PGID 级联杀掉 uv run 的子进程
  # shellcheck disable=SC2068
  setsid nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 &
  echo $! >"$pidfile"
}

echo ""
echo "[1/4] 启动后端主服务(端口 9380)..."
start server \
  env PYTHONPATH=. LLM_TIMEOUT_SECONDS=20 LITELLM_LOCAL_MODEL_COST_MAP=true \
  uv run python api/ragflow_server.py --init-superuser

echo ""
echo "[2/4] 启动 task_executor(解析消费端)..."
start executor \
  env PYTHONPATH=. LLM_TIMEOUT_SECONDS=60 LITELLM_LOCAL_MODEL_COST_MAP=true \
  uv run python rag/svr/task_executor.py

# ---------- 3) 前端 vite dev ----------
echo ""
echo "[3/4] 启动前端 vite dev(端口 9222)..."
if [ "${1:-}" != "--no-web" ]; then
  if [ -d "web/node_modules" ]; then
    start web bash -c 'cd web && npm run dev'
  else
    echo "  ⚠️  web/node_modules 不存在, 先 npm install:"
    echo "      cd web && npm install"
  fi
else
  echo "  (跳过, --no-web)"
fi

# ---------- 4) 等待就绪 ----------
echo ""
echo "[4/4] 等待服务就绪..."

wait_http() { # wait_http <name> <url> <timeout_s>
  local name="$1" url="$2" to="$3" i=0
  printf "  等待 %s 就绪 " "$name"
  until curl -s -o /dev/null -m 2 "$url" 2>/dev/null; do
    printf "."
    sleep 1
    i=$((i+1))
    if [ "$i" -ge "$to" ]; then printf "\n  ✗ %s 超时(${to}s)\n" "$name"; return 1; fi
  done
  printf " ✓ (%ss)\n" "$i"
}

wait_http "后端 9380" "http://127.0.0.1:9380/v1/user/info" 60
# vite dev 探测根路径
if [ "${1:-}" != "--no-web" ] && [ -d "web/node_modules" ]; then
  wait_http "前端 9222" "http://127.0.0.1:9222/" 60
fi

echo ""
echo "==================== 启动完成 ===================="
echo "  后端 API:   http://127.0.0.1:9380"
echo "  前端 UI:    http://127.0.0.1:9222"
echo "  日志:       $LOG_DIR/{server,executor,web}.log"
echo "  PID 文件:   $LOG_DIR/{server,executor,web}.pid"
echo "  停止:       bash stop_dev.sh"
echo "=================================================="