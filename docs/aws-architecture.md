# Arquitectura futura en AWS — comparación de alternativas

Estado: **propuesta. No hay recursos AWS creados ni conexiones.** La elección definitiva depende de los créditos, la cuenta y el presupuesto autorizado de RCKT, que todavía no están habilitados.

Principio de portabilidad: la misma imagen Docker (`Dockerfile`), las mismas variables de entorno y la misma base externa (Supabase licenciado). Cambiar de plataforma no exige reconstruir la aplicación.

## Lo que no cambia en ninguna alternativa

- **Base de datos:** Supabase licenciado, externo. **No se migra a RDS.** La aplicación se conecta por `DATABASE_URL` con SSL.
- **Roles:** `lacusine_app` (API y worker) y `lacusine_admin` (conciliación). Nunca el propietario.
- **Secretos:** en el servicio de secretos de AWS (Secrets Manager o SSM Parameter Store), inyectados como variables. Nunca en la imagen, en el repositorio ni en archivos de infraestructura.
- **Interruptores:** `META_SEND_ENABLED=false`, `WHATSAPP_DRY_RUN=true`, `ANTHROPIC_ENABLED=false`, `AGENT_DRY_RUN=true`, `WORKER_ENABLED=false` hasta la autorización del piloto.
- **Dos procesos:** API y worker separados. El worker nunca se ejecuta dentro de FastAPI.
- **HTTPS:** el webhook de Meta exige una URL HTTPS pública.

## Alternativa A — Amazon EC2 con Docker

Una instancia Linux que ejecuta los contenedores de API y worker con Docker Compose (o `docker run` con el mismo comando).

| Aspecto | Evaluación |
|---|---|
| Facilidad de despliegue | Alta. Un host, la misma imagen y los mismos comandos que en local |
| Mantenimiento del sistema operativo | Responsabilidad de RCKT: parches, actualizaciones de seguridad, Docker |
| Seguridad | Grupo de seguridad con entrada solo HTTPS y SSH restringido o sustituido por SSM Session Manager. Sin puertos de base de datos abiertos |
| HTTPS | Terminación con Caddy o nginx en el propio host, o con un balanceador (ver costos) |
| Recuperación | Reinicio automático con `restart: unless-stopped`. Reconstruir desde una AMI o desde el repositorio. Sin alta disponibilidad: un solo host |
| Respaldos | Respaldos del host (instantánea del volumen) para lo que quede en él. Los datos viven en Supabase, con su propio respaldo |
| Disponibilidad | Un único punto de fallo. Aceptable para un piloto; insuficiente para producción sin tolerancia |
| Crecimiento | Vertical (tipo de instancia) u horizontal manual |

## Alternativa B — Amazon ECS con AWS Fargate

Servicios ECS separados para API y worker, con imágenes en Amazon ECR, sin gestionar servidores.

| Componente | Función | Observación |
|---|---|---|
| Amazon ECR | Registro de la imagen | Coste por almacenamiento y transferencia |
| Servicio ECS (API) | Ejecuta FastAPI | Tareas Fargate dimensionadas en vCPU y memoria |
| Servicio ECS (worker) | Ejecuta el worker | **Se factura continuamente**, aunque esté en espera |
| Balanceador de aplicación (ALB) | HTTPS y enrutado al servicio API | Coste fijo por hora más consumo de capacidad |
| AWS Certificate Manager | Certificado HTTPS | Sin coste directo para el certificado público en el balanceador |
| Secrets Manager o SSM | Secretos inyectados | Coste por secreto y por llamada, según el servicio |
| CloudWatch Logs | Logs | Coste por ingestión y retención |
| IAM | Roles de tarea de mínimo privilegio | Sin coste directo |
| GitHub Actions con OIDC | Despliegue sin claves de larga duración | Solo cuando se autorice |
| NAT Gateway | Salida a internet desde subredes privadas | **Coste fijo por hora y por datos procesados.** Suele ser el componente que más sorprende |

## Comparación

| Criterio | EC2 con Docker | ECS con Fargate |
|---|---|---|
| Costo fijo mensual | Bajo: una instancia | Mayor: ALB, y NAT si se usan subredes privadas |
| Costo variable | Bajo | Proporcional al uso de tareas y de datos |
| Complejidad de operación | Media: sistema operativo a cargo de RCKT | Media-alta: más piezas (ECS, ECR, ALB, IAM, subredes) |
| Escalabilidad | Manual y limitada | Automática por servicio |
| Seguridad | Depende de la configuración del host | Menos superficie: sin SO que parchear |
| Recuperación | Reemplazar la instancia | Reemplazo automático de tareas |
| Despliegues controlados | Manuales o con scripts | Despliegues por servicio, con rollback a la revisión anterior |

**No asumimos que Fargate sea más barato.** Para un piloto con volumen bajo y un único worker, la factura fija de un balanceador y de una salida a internet puede superar el coste de una instancia pequeña. Solo con cifras reales de volumen se puede decidir.

## Costos: componentes y supuestos

Los precios no se han consultado ni se incluyen aquí. Deben tomarse de la calculadora de precios de AWS en la región elegida, con la fecha de presupuesto.

Componentes que pueden generar coste, sin importar la alternativa:

- Cómputo continuo (instancia o tareas), **incluido el worker en espera**.
- Almacenamiento de volúmenes y de imágenes en el registro.
- Transferencia de datos hacia internet y entre zonas de disponibilidad.
- Balanceador, si se usa.
- NAT Gateway, si se usan subredes privadas.
- Logs y su retención.
- Secretos administrados, por unidad y por llamada.

Supuestos **no confirmados** que condicionan la decisión:

1. Volumen de mensajes por mes. No está medido.
2. Número de líneas de WhatsApp. El MVP es de una línea.
3. Disponibilidad exigida (horas, tolerancia a caídas).
4. Región de AWS. Sugerencia a validar: la más cercana a los usuarios y compatible con el proveedor de Supabase.
5. Créditos disponibles y su vigencia.
6. Si el piloto requiere alta disponibilidad o basta con un host.

## Recomendación

**Primer paso: Alternativa A (EC2 con Docker).** Motivo: la misma imagen y los mismos comandos que en local, el coste fijo más bajo y la operación más simple para un piloto de una línea. Se acepta el riesgo de un único host y se mitiga con reinicio automático, respaldos del host y Supabase como almacén de datos.

**Migración a Alternativa B cuando se cumpla alguna condición:**

- Se necesiten dos o más instancias del API o del worker.
- Haya un requisito de disponibilidad que un solo host no cumpla.
- El volumen justifique el coste fijo del balanceador y del NAT.

La migración no cambia la aplicación: la misma imagen pasa a ser una tarea de ECS.

## Decisión pendiente

La elección definitiva queda pendiente de:

- Habilitación de la cuenta AWS y de los créditos.
- Presupuesto mensual autorizado por RCKT.
- Medición real de volumen en el piloto.

Ninguna acción de esta sección ha consumido créditos ni creado recursos.
