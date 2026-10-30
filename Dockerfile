# Imagen única para FastAPI, worker y migraciones. El comando de cada servicio lo fija docker-compose.yml
# (o la plataforma de despliegue). No contiene secretos, ni .env, ni documentación, ni el catálogo original.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 lacusine \
 && useradd --system --uid 10001 --gid lacusine --home-dir /app --shell /usr/sbin/nologin lacusine

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY supabase ./supabase

USER lacusine

EXPOSE 8000
STOPSIGNAL SIGTERM

# Liveness: no depende de la base. Readiness (/ready) se usa para monitoreo, no como healthcheck de arranque.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
