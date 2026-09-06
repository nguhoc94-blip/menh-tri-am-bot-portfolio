FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System libs required by opencv-python-headless and python-magic
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY assets ./assets
COPY knowledge ./knowledge
COPY rulebooks ./rulebooks
COPY prompts ./prompts
COPY sql ./sql
COPY templates ./templates
COPY scripts ./scripts

RUN chmod +x scripts/render-start.sh scripts/render-worker-start.sh

EXPOSE 8000

ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=3).read()"

CMD ["bash", "scripts/render-start.sh"]
