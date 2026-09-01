FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production
ENV HOST=0.0.0.0
ENV PORT=8000
ENV RELOAD=false
ENV COOKIE_SECURE=true
ENV FRONTEND_DIR=/app/frontend

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend ./frontend

RUN pip install --no-cache-dir ".[api]"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '8000') + '/health').read()"

CMD ["arranger-api"]
