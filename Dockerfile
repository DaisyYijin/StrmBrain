# ==================== 构建阶段 ====================
FROM python:3.12-slim AS builder

WORKDIR /build

# 安装编译依赖（bcrypt 等需要 gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ==================== 运行阶段 ====================
FROM python:3.12-slim

LABEL maintainer="STRMhub"
LABEL description="115 网盘 STRM 生成工具"

WORKDIR /app

# 安装 ffmpeg/ffprobe（Linux 版本，media_probe.py 会通过系统 PATH 自动发现）
# 同时安装 tzdata 设置时区
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制已安装的 Python 依赖
COPY --from=builder /install /usr/local

# 复制项目代码
COPY backend/ ./

# 创建数据目录
RUN mkdir -p /app/data /app/config /app/log /media

# 环境变量默认值
ENV HOST=0.0.0.0 \
    PORT=6060 \
    AUTH_ENABLED=true \
    ADMIN_USERNAME=admin \
    ADMIN_PASSWORD=admin123 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 暴露端口（6060 主服务，6086 Emby 反代 302 播放）
EXPOSE 6060 6086

# 数据持久化（运行数据、配置、日志、STRM 媒体输出）
VOLUME ["/app/data", "/app/config", "/app/log", "/media"]

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:6060/api/health')" || exit 1

# 启动命令
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6060"]
