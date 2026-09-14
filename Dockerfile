FROM python:3.12-slim

WORKDIR /app
COPY . /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOST=0.0.0.0

# 纯标准库，无第三方依赖，故无 pip install 步骤
RUN useradd -m -u 10001 appuser \
 && mkdir -p /app/logs /app/uploads \
 && chown -R appuser /app
USER appuser

# 同一镜像三种角色：app.py(API) / worker.py(识别 worker) / healthd.py(健康检查)
CMD ["python3", "app.py"]
