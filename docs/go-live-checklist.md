# Lista de verificación de salida a producción

Esta lista es una **puerta**: ninguna casilla se marca por intención. Cada una exige evidencia. Mientras una esté abierta, el sistema no envía mensajes reales ni llama a Claude en producción.

Estado actual: **todas las casillas de integración están abiertas.** Las de código están cumplidas en la rama de Fase 5, verificadas con las pruebas del repositorio.

## 1. Código y pruebas (verificable en el repositorio)

- [ ] Pruebas unitarias aprobadas sin base de datos y sin red.
- [ ] Pruebas de integración aprobadas contra PostgreSQL local protegido por el guard.
- [ ] Flujo extremo a extremo simulado aprobado (webhook firmado → worker → IA simulada → outbox → envío simulado → estado entregado).
- [ ] `pip check` limpio en `.venv`.
- [ ] `git diff --check` y `git diff --cached --check` limpios.
- [ ] Ningún secreto en el stage (escaneo de claves, tokens y URLs con credenciales).
- [ ] Rama de la fase revisada y PR en borrador; ninguna fusión sin aprobación.

## 2. Configuración (en el panel de Render, nunca en Git)

- [ ] Los valores de `META_APP_SECRET`, `META_VERIFY_TOKEN`, `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `DATABASE_URL` y `ANTHROPIC_API_KEY` se introdujeron en el panel.
- [ ] `META_GRAPH_API_VERSION` es la versión vigente verificada en la documentación de Meta el día de la puesta en marcha.
- [ ] `WHATSAPP_ALLOWED_RECIPIENTS` contiene solo números autorizados por escrito.
- [ ] `ANTHROPIC_MODEL` y las tarifas (`ANTHROPIC_PRICE_*`) están definidas por el equipo y revisadas contra la documentación de Anthropic el día del presupuesto.
- [ ] Presupuestos diario, mensual y por interacción definidos con un responsable que los apruebe.
- [ ] `AGENT_KILL_SWITCH` probado: al ponerlo en `true`, el agente se detiene sin llamadas.

## 3. Base de datos (Supabase)

- [ ] Las migraciones 1 a 13 se aplicaron en un proyecto de prueba aislado sin errores, con `python -m app.db.migrate`.
- [ ] Roles de login creados fuera del repositorio y asociados a `lacusine_backend` y `lacusine_admin_ops`. Ninguno es `SUPERUSER`, `BYPASSRLS` ni propietario del esquema.
- [ ] La aplicación no se conecta como `postgres` ni con `service_role` (comprobar `DATABASE_URL` y `LACUSINE_ADMIN_DATABASE_URL` en el panel).
- [ ] Permisos verificados con conexiones reales de cada rol en el proyecto de prueba: denegaciones de `anon`, `authenticated` y de la auditoría.
- [ ] Respaldo o punto de restauración confirmado antes de aplicar en producción.
- [ ] Rol del backend creado con mínimo privilegio y verificado.
- [ ] RLS activa en todas las tablas; `anon` y `authenticated` sin privilegios (verificado con consultas de solo lectura).
- [ ] Cadena de conexión elegida según [supabase-deployment.md](supabase-deployment.md), con SSL.
- [ ] `/ready` responde 200 desde la plataforma de destino (AWS; Render queda descartado).

## 4. Meta y WhatsApp

- [ ] Diagnóstico de coexistencia completado por el responsable de la cuenta (ver [whatsapp-integration.md](whatsapp-integration.md)).
- [ ] Permisos de la aplicación verificados y webhook suscrito a `messages` de la cuenta correcta.
- [ ] Número de prueba de Meta usado para la primera verificación, autorizado por escrito.
- [ ] Webhook público en HTTPS verificado con el handshake de Meta.
- [ ] Estados de entrega recibidos y aplicados en el número de prueba.
- [ ] Plantillas: decisión comercial registrada. Mientras no exista una plantilla aprobada, las conversaciones fuera de ventana quedan bloqueadas.

## 5. Operación

- [ ] Background Worker desplegado con `WORKER_ENABLED=false` y en estado de espera (`worker_standby`), sin reinicios continuos en el panel.
- [ ] Prueba de arranque del worker con procesos reales (`tests/test_worker_process.py`) ejecutada en el entorno de despliegue.
- [ ] Procedimiento de revisión de `manual_review` y de reservas `uncertain` definido, con responsable, usando `python -m app.admin.cli` (ver [worker.md](worker.md)).
- [ ] Procedimiento de conciliación de envíos `uncertain` definido, con responsable. Ningún envío `uncertain` se resuelve sin evidencia externa (referencia de Meta o prueba de no facturación).
- [ ] Unidad de `timestamp` de Meta verificada con un mensaje real de prueba (la ventana depende de ella).
- [ ] Ventana de 24 horas probada con un mensaje antiguo de la cola y con un mensaje reciente.
- [ ] Procedimiento de carga de catálogo definido sin importación automática a la base real (ver [catalogo-operacion.md](catalogo-operacion.md)).
- [ ] Monitoreo de `/health`, `/ready` y del conteo de estados en la base.
- [ ] Plan de rollback documentado y ensayado (redespliegue del commit anterior).
- [ ] Autoriza el equipo legal la retención de `WEBHOOK_EVENT_RETENTION_DAYS` y de los contenidos de mensajes.
- [ ] Las políticas marcadas como pendientes de validación legal no se presentan a clientes como afirmaciones definitivas.

## 6. Piloto controlado (solo con autorización expresa)

- [ ] `META_SEND_ENABLED=true` y `WHATSAPP_DRY_RUN=false` activados **solo** para el piloto, con la lista de destinatarios reducida.
- [ ] `WORKER_ENABLED=true` tras confirmar todo lo anterior.
- [ ] Un responsable observa los primeros envíos y los estados de entrega.
- [ ] Criterio de parada definido: cualquier `uncertain` no resuelto o error 401/403 detiene el piloto.

## Qué no cubre esta lista

- Validación jurídica de las políticas comerciales. Corresponde a LCI.
- Costos reales de Meta, Anthropic, Supabase y AWS. Verificar en sus páginas de precios.
- Elección final de plataforma (EC2 o ECS): ver [aws-architecture.md](aws-architecture.md).
- Pruebas de carga a volumen de producción.
- Cumplimiento de protección de datos más allá de la retención y los accesos descritos en el repositorio.

## 7. Demostración en Render Free (solo destino de demostración)

- [ ] Un único Web Service Docker, plan Free, auto-deploy desactivado, sin worker adicional.
- [ ] Variables de Render Environment definidas en el panel; ningún valor en el repositorio.
- [ ] `DEMO_ENABLED=true` solo en este servicio; `META_SEND_ENABLED=false`, `WHATSAPP_DRY_RUN=true`, `ANTHROPIC_ENABLED=false`, `AGENT_DRY_RUN=true`.
- [ ] Base de demostración aislada, con migraciones 1 a 13 y roles de mínimo privilegio. Sin datos reales.
- [ ] Token de `/demo` generado fuera del repositorio y revocable.
- [ ] Limitaciones de suspensión comunicadas: no hay atención 24/7 ni procesamiento continuo.

## 8. AWS (pendiente de créditos y autorización)

- [ ] Elección EC2 o ECS registrada en [aws-architecture.md](aws-architecture.md), con el presupuesto autorizado.
- [ ] Dos servicios con la misma imagen: API (`APP_RUN_MODE=web`) y worker (`APP_RUN_MODE=worker`).
- [ ] Secretos en Secrets Manager o SSM; logs en CloudWatch; rol de mínimo privilegio.
- [ ] Migraciones aplicadas como paso explícito, nunca en el arranque.
