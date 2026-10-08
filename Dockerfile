FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 shop \
    && useradd --uid 10001 --gid shop --no-create-home shop \
    && mkdir -p /app/data /app/backups \
    && chown -R shop:shop /app/data /app/backups

COPY shop ./shop
COPY scripts ./scripts
USER shop

CMD ["python", "-m", "shop"]
