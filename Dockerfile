# 微信 AI 助手 · 容器镜像
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WXBOT_HOST=0.0.0.0 \
    WXBOT_PORT=8000 \
    WXBOT_DATA_DIR=/app/data \
    WXBOT_NO_BROWSER=1

RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -r requirements.txt --index-url "$PIP_INDEX_URL" \
    || pip install --no-cache-dir -r requirements.txt --index-url https://pypi.org/simple

COPY src/ ./src/

RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["python", "-m", "src.server"]
