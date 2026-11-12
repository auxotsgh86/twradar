FROM python:3.12-slim
ENV TWRADAR_HOME=/data PYTHONUNBUFFERED=1 TZ=Asia/Taipei
WORKDIR /app
RUN pip install --no-cache-dir certifi
COPY twradar /app/twradar
VOLUME /data
EXPOSE 8765
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/v1/health')"
CMD ["python", "-m", "twradar", "serve"]
