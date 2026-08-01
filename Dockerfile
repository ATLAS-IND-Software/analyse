FROM python:3.12-slim

ARG APP_VERSION=0.0.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION} \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py VERSION ./
COPY templates ./templates
COPY static ./static

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers ${GUNICORN_WORKERS:-1} --threads 4 --timeout 180 --graceful-timeout 30 --keep-alive 5 --max-requests 250 --max-requests-jitter 25 --worker-tmp-dir /dev/shm --access-logfile - main:app"]
