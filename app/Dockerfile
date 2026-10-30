FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8
WORKDIR /app
COPY app/*.py /app/app/
COPY web /app/web
COPY icon.png /app/icon.png
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV DATA_DIR=/data PORT=9207 PYTHONDONTWRITEBYTECODE=1
EXPOSE 9207
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','9207'),timeout=4)"
CMD ["python3", "/app/app/server.py"]
