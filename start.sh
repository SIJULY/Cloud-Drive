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

# 启动 Flask (使用 Gunicorn)
# -w 4: 启动 4 个工作进程
# --threads 10: 每个进程开启 10 个线程 (处理上传下载非常有效)
# --timeout 0: 永不超时，直到上传完成
exec gunicorn -w 4 --threads 10 -b 0.0.0.0:5000 --timeout 0 app:app
