#!/usr/bin/env bash
# my-ragflow 一键停止脚本
# stop 掉 launch_dev.sh 用 setsid 拉起的进程组(PGID = pid 文件里的数值)
# 用 kill -- -PGID 级联杀掉整个进程组(含 uv run 的子 python), 避免只杀父进程、留子进程残留
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/logs"

for name in server executor web; do
  pidfile="$LOG_DIR/$name.pid"
  if [ ! -f "$pidfile" ]; then
    echo "$name: 无 pid 文件(未通过本脚本启动)"
    continue
  fi
  pgid=$(cat "$pidfile")
  if ! echo "$pgid" | grep -qE '^[0-9]+$'; then
    echo "$name: pid 文件内容异常 ($pgid), 跳过"
    continue
  fi
  if kill -0 -- "-$pgid" 2>/dev/null; then
    # 先给进程组发 SIGTERM
    kill -- "-$pgid" 2>/dev/null
    echo "已发送停止信号给进程组 $pgid ($name)"
    # 等最多 5s, 未退则 SIGKILL
    for _ in 1 2 3 4 5; do
      sleep 1
      kill -0 -- "-$pgid" 2>/dev/null || break
    done
    if kill -0 -- "-$pgid" 2>/dev/null; then
      kill -9 -- "-$pgid" 2>/dev/null
      echo "  未正常退出, 已强杀 ($name)"
    else
      echo "  已退出 ($name)"
    fi
  else
    # 进程组已不存在的兜底: 单进程形式
    if kill -0 "$pgid" 2>/dev/null; then
      kill -9 "$pgid" 2>/dev/null
      echo "已清理孤立进程 $pgid ($name)"
    else
      echo "$name: 进程组 $pgid 未在运行"
    fi
  fi
  rm -f "$pidfile"
done

echo ""
echo "停止完成"
echo "(启动脚本已在 setsid 下运行, 未留 uv run/python/vite 子进程残留)"