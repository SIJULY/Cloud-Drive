FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖 (curl 用于健康检查)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单并安装 Python 包
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 先复制下载脚本并运行：把 Vue/Tailwind/FontAwesome 下载到 /app/static
# 下载失败直接让构建失败，别带着缺资源的镜像上线
COPY download_assets.py .
RUN python download_assets.py

# 复制其余项目代码（放在后面，避免代码变动导致重复下载资源）
COPY . .

# 创建数据挂载点
VOLUME /app/storage
VOLUME /app/trash
VOLUME /app/shares

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/healthz || exit 1

COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
