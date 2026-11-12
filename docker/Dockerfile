FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    TRIM_PKGVAR=/data \
    TRIM_DATA_ACCESSIBLE_PATHS=/media \
    VT_BIND_HOST=0.0.0.0 \
    VT_BIND_PORT=18080

# 与 fpk 安装包使用同一套程序：ffmpeg 由镜像提供，后端只用 Python 标准库
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/video-tool
COPY fpk/app/ /opt/video-tool/
RUN mkdir -p /data /media

VOLUME ["/media", "/data"]
EXPOSE 18080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/api/config', timeout=3)"

CMD ["python", "/opt/video-tool/server/app.py", "--host", "0.0.0.0", "--port", "18080"]
