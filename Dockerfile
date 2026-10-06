# Hybrid Leads in a container: any small server, free or paid. See "Deploy" in README.md.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ITLEADS_HOME=/data ITLEADS_HOST=0.0.0.0 ITLEADS_PORT=8765
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY itleads ./itleads
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['ITLEADS_PORT'], timeout=4)"
CMD ["python", "-m", "itleads", "serve"]
