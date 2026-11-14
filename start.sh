#!/bin/bash
set -e

# 等 Redis 就绪再起 worker，避免 Celery 连不上反复报错
for i in $(seq 1 30); do
    if python3 -c "import os, socket; from urllib.parse import urlparse; u=urlparse(os.environ.get('REDIS_URL','redis://localhost:6379/0')); s=socket.create_connection((u.hostname or 'localhost', u.port or 6379), timeout=2); s.close()" 2>/dev/null; then
        break
    fi
    echo "等待 Redis... ($i/30)"
    sleep 1
done

# 启动 Celery Worker (后台运行)
celery -A tasks worker --loglevel=info &

# 启动 aria2（离线下载）：RPC 只监听本机，密钥默认用 SECRET_KEY。
# 下载完成/出错时调 /app/aria2-hook.py 把文件移进网盘、更新任务状态。
mkdir -p /app/shares/downloads
if command -v aria2c >/dev/null 2>&1; then
    aria2c --enable-rpc --rpc-listen-all=false --rpc-listen-port=6800 \
        --rpc-secret="${ARIA2_SECRET:-$SECRET_KEY}" \
        --dir=/app/shares/downloads \
        --on-download-complete=/app/aria2-hook.py \
        --on-download-error=/app/aria2-hook.py \
        --daemon=true --log=/app/shares/aria2.log || echo "aria2 启动失败，离线下载不可用"
fi

# 启动 Flask (使用 Gunicorn)
# -w 4: 启动 4 个工作进程
# --threads 10: 每个进程开启 10 个线程 (处理上传下载非常有效)
# --timeout 0: 永不超时，直到上传完成
exec gunicorn -w 4 --threads 10 -b 0.0.0.0:5000 --timeout 0 app:app
