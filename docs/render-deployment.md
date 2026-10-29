# Despliegue en Render — preparación (no desplegado)

Estado: **preparado, no ejecutado.** El archivo [`render.yaml`](../render.yaml) describe los servicios, pero no crea nada hasta que el equipo lo use desde el panel de Render con autorización expresa.

## Tres piezas, tres responsabilidades

| Pieza | Qué es | Qué hace | Dónde está |
|---|---|---|---|
| **Render Web Service** | Proceso HTTP | Recibe el webhook de Meta, valida la firma y persiste el evento. Expone `/health` y `/ready`. | Render |
| **Render Background Worker** | Proceso continuo | Consume `webhook_events`, ejecuta el agente, escribe el outbox y despacha las respuestas. | Render |
| **Supabase PostgreSQL** | Base de datos externa | Guarda eventos, conversaciones, colas, respuestas y reservas. | Supabase |

**El worker no depende de tráfico HTTP.** Procesa la cola en un bucle propio. Por eso no puede ejecutarse dentro de FastAPI ni con auto-reload.

## Servicios definidos

### Web Service `lacusine-api`

- Comando de inicio: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- Puerto dinámico: Render asigna `$PORT`. No se fija un puerto en el código.
- Health check de despliegue: `/health` (liveness, sin dependencias). Así un fallo temporal de la base no impide desplegar.
- `/ready` comprueba PostgreSQL. Se usa para monitoreo, no como puerta de despliegue.
- Apagado limpio: uvicorn cierra al recibir la señal de Render.

### Background Worker `lacusine-worker`

- Comando de inicio: `python -m app.worker.main`.
- **No admite `healthCheckPath`.** Render solo lo ofrece para web services. La supervisión del worker se hace por sus logs (`worker_started`, `dispatch_cycle`, `job_finished`) y por el conteo de la base.
- Apagado: Render envía SIGTERM. El worker deja de reclamar trabajos nuevos y espera a los que están en curso (`Worker.shutdown`).
- Con `WORKER_ENABLED=false` (valor en el blueprint) el proceso sale sin consumir la cola. Se activa a propósito, en la fase de salida.

Un solo Background Worker ejecuta el procesamiento de eventos **y** el despacho de salidas, porque comparten ciclo de vida, base y supervisión. Un segundo servicio solo aportaría costo; no se añade.

## Planes y costos

- `plan: starter` está como valor de partida. **Validar el plan y su costo en el panel antes de crear los servicios.** Este repositorio no declara precios.
- El plan gratuito **suspende** el servicio por inactividad. Eso es incompatible con recibir webhooks y procesar una cola 24/7. No debe usarse para operación comercial.
- Cada servicio se factura por separado. Un worker encendido consume recursos aunque no haya mensajes.

## Despliegues automáticos

- `autoDeployTrigger: off` en ambos servicios. Ningún push a `main` ni a una rama de desarrollo despliega solo.
- El despliegue se hace de forma manual, desde el panel, sobre un commit concreto.

## Secretos

- En `render.yaml`, cada secreto aparece con `sync: false`. Render pide el valor al crear el Blueprint y no lo sobrescribe en sincronizaciones posteriores.
- Ningún secreto tiene valor literal en el archivo. Una prueba del repositorio lo verifica.
- Los valores se introducen en el panel de Render, no en GitHub ni en Supabase. Ver [environment-variables.md](environment-variables.md).

## Procedimiento (cuando haya autorización)

1. Confirmar que el commit a desplegar pasó todas las pruebas y que la rama es la aprobada.
2. Conectar el repositorio GitHub en Render (la conexión la autoriza un administrador de la cuenta de Render; no se hace desde este repositorio).
3. Crear el Blueprint apuntando a `render.yaml` en la rama aprobada.
4. Introducir los valores de los secretos en el panel. Dejar `WORKER_ENABLED=false` y los interruptores de envío y de IA en su valor seguro.
5. Esperar a que el Web Service responda en `/health`.
6. Verificar `/ready` (conexión a Supabase).
7. Verificar que el worker arranca y escribe `worker_started` sin errores de configuración.
8. Revisar la lista de [go-live-checklist.md](go-live-checklist.md) antes de activar cualquier interruptor de envío.

## Rollback

- Desde el panel de Render: redesplegar el commit anterior conocido.
- Las migraciones de Supabase **no se revierten automáticamente**. Son hacia adelante. Un cambio de esquema malo se corrige con una nueva migración o con restauración desde respaldo (ver [supabase-deployment.md](supabase-deployment.md)).

## Qué no hace esta preparación

- No crea servicios, no activa facturación y no conecta el repositorio.
- No ejecuta el worker ni el webhook en ningún entorno.
- No configura dominio ni certificados. El webhook de Meta necesita una URL HTTPS pública, que se define al configurar Meta (ver [whatsapp-integration.md](whatsapp-integration.md)).
