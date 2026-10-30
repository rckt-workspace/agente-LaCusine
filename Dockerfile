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

# Liveness: no depende de la base. Solo aplica al modo web; el worker no expone HTTP. Readiness (/ready) se usa para monitoreo, no como healthcheck de arranque.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3   CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/health', timeout=3).status == 200 else 1)"

# Modo de ejecución por variable: web (defecto), worker o demo-combined. Ver app/runtime/entrypoint.py.
ENV APP_RUN_MODE=web
CMD ["python", "-m", "app.runtime.entrypoint"]
