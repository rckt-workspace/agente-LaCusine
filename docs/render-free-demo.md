# Demostración en Render Free — procedimiento y límites

Estado: **preparado, no desplegado.** No se ha creado ningún servicio en Render ni ninguna base de datos. Esta guía se usa solo cuando RCKT autorice crear la cuenta gratuita para mostrar el producto a la dirección.

## Qué es y qué no es

- **Es:** un único Web Service Docker en plan **Free**, que ejecuta la API y el worker juntos (`APP_RUN_MODE=demo-combined`), con datos **sintéticos** y proveedores **simulados**.
- **No es:** un entorno de producción, ni una atención real a clientes, ni 24/7.

## Limitaciones obligatorias

| Límite | Consecuencia |
|---|---|
| El plan Free **suspende** el servicio tras 15 minutos sin tráfico entrante | La API y el worker se detienen al suspenderse. Los mensajes no se procesan mientras está suspendido |
| Recursos aproximados de 0,1 CPU y 512 MB de RAM | Un solo proceso de API y un worker de concurrencia 1. Ver la medición más abajo |
| El sistema de archivos del contenedor es efímero | No se guarda nada en el contenedor. Todos los datos están en PostgreSQL externo |
| PostgreSQL no va dentro del servicio | Requiere una base externa (ver sección 4) |
| Sin pings artificiales | Está prohibido mantener el servicio despierto para evadir la suspensión. La demo no promete disponibilidad |

**Consecuencia para la presentación:** la demo se inicia manualmente antes de la reunión. Si estuvo suspendida, la primera petición tarda en responder mientras el servicio se reactiva.

## Medición con los límites de Render Free

Medido en esta máquina con `docker run --memory=512m --cpus=0.1`, PostgreSQL local, modo `demo-combined`:

| Medida | Resultado |
|---|---|
| Procesos | API (uvicorn) y worker, un solo worker |
| Memoria en uso tras un flujo completo | ~130 MiB de 512 MiB |
| CPU en reposo | ~0,6 % |
| Tiempo hasta responder `/health` bajo 0,1 CPU | Entre 36 y 84 segundos en las mediciones realizadas. Depende de la CPU: la importación de la aplicación tarda unos 40 segundos con 0,1 CPU. Precompilar el bytecode no produjo una mejora medible |
| Consumo durante el flujo completo (mensajes, worker, transferencias) | 100 a 130 MiB de memoria; CPU entre 0,6 % y 10 % |

**Conclusión:** la memoria y la CPU caben de forma estable en el límite. El arranque es lento (entre 36 y 84 segundos medidos). Si Render Free reinicia el servicio por tiempo de arranque, el `healthCheckPath` a `/health` (ya configurado) debe dar más margen en el panel.

**Qué NO se ha medido:** el comportamiento con carga sostenida de muchos visitantes ni varias horas de ejecución en Render real. Las cifras son de un contenedor local con los mismos límites.

## 1. Crear el Web Service gratuito

1. En Render, **New → Blueprint** o **New → Web Service**.
2. Conectar el repositorio autorizado `rckt-workspace/agente-LaCusine` y elegir la rama **aprobada** (no `main` hasta que se fusione).
3. Runtime: **Docker**. Dockerfile: `./Dockerfile`. Contexto: `.`
4. Plan: **Free**.
5. Desactivar el **auto-deploy** (ya viene desactivado en `render.yaml`).
6. Health check path: `/health`.

Si se usa el Blueprint, `render.yaml` define el servicio `lacusine-demo`. Revisar que solo aparezca **un** servicio y ningún worker adicional.

## 2. Variables en Render Environment

Introducir los valores en el panel de Render. **Nunca** en el repositorio. Solo nombres:

| Variable | Origen | Valor / criterio |
|---|---|---|
| `APP_RUN_MODE` | `render.yaml` | `demo-combined` |
| `DEMO_ENABLED` | `render.yaml` | `true` (solo en este destino) |
| `DEMO_PUBLIC_ORIGIN` | panel | `https://<servicio>.onrender.com`, exacto, sin ruta. Obligatoria: sin ella la demo no arranca. Se conoce tras crear el servicio |
| `DEMO_TOKEN` | panel (secreto) | Token aleatorio de al menos 24 caracteres, generado fuera del repositorio |
| `DATABASE_URL` | panel (secreto) | PostgreSQL de demostración, con rol `lacusine_app` y SSL |
| `META_APP_SECRET` | panel (secreto) | Valor de la aplicación de prueba, si se usa el webhook |
| `META_VERIFY_TOKEN` | panel (secreto) | Token de verificación de la aplicación de prueba |
| `META_PHONE_NUMBER_ID` | panel (secreto) | Identificador de línea **de prueba** o sintético |
| `META_GRAPH_API_VERSION` | `render.yaml` | `v26.0` |
| `WORKER_ENABLED` | `render.yaml` | `true` (procesamiento interno con simuladores) |
| `WORKER_MAX_CONCURRENCY` | `render.yaml` | `1` |
| `META_SEND_ENABLED` | `render.yaml` | `false` **obligatorio** |
| `WHATSAPP_DRY_RUN` | `render.yaml` | `true` **obligatorio** |
| `ANTHROPIC_ENABLED` | `render.yaml` | `false` **obligatorio** |
| `AGENT_DRY_RUN` | `render.yaml` | `true` **obligatorio** |
| `ADVISOR_NOTIFIER` | `render.yaml` | `simulated` |

**Comprobación antes de iniciar:** si `DEMO_ENABLED=true` y falta `DEMO_TOKEN`, `DATABASE_URL` o `META_PHONE_NUMBER_ID`, el servicio **no arranca** (falla cerrado). Si `META_SEND_ENABLED=true` con la demo activa, tampoco arranca.

## 3. Migraciones (antes del primer arranque)

Las migraciones **no** se ejecutan al arrancar. Se aplican una vez, desde un equipo local, contra la base de demostración:

```powershell
$env:DATABASE_URL = "<URL del propietario de la base de demostración, solo en esta sesión>"
.venv\Scripts\python.exe -m app.db.migrate --dir supabase/migrations            # ensayo
.venv\Scripts\python.exe -m app.db.migrate --dir supabase/migrations --apply    # aplicar
Remove-Item Env:DATABASE_URL
```

Después crear los roles de login a mano, con las sentencias de [supabase-deployment.md](supabase-deployment.md) (sección de roles). `app.db.dev_roles` se niega a ejecutarse contra un host que no sea local.

## 4. PostgreSQL para la demostración

Opción preferida: **un proyecto de base de datos dedicado y aislado para la demo**, sin relación con la base empresarial de La Cuisine. Verificar en la documentación vigente del proveedor el plan gratuito, sus límites y su política de pausa por inactividad antes de usarlo.

Alternativa: un PostgreSQL gratuito temporal de Render. Verificar su caducidad en el panel antes de elegirlo.

**No** usar la base empresarial. **No** usar credenciales de propietario en la aplicación.

## 5. Datos de demostración

- Solo sintéticos: prospectos ficticios, teléfonos inventados (prefijo `5730` más ocho dígitos), productos de ejemplo.
- Para la demo, las transferencias y los avisos son simulados. No llega ningún correo ni mensaje a nadie.
- No cargar el catálogo comercial real. Si se usa un subconjunto, debe estar expresamente autorizado por escrito.

## 6. Iniciar la demostración

1. Comprobar en Render que el servicio está **Live** y que `/health` responde 200.
2. Abrir `https://<servicio>.onrender.com/demo`.
3. Introducir `DEMO_TOKEN`. Sin token no se muestra ningún dato.
4. Enviar un mensaje sintético, opcionalmente con la marca de anuncio, y observar la cola, la respuesta y el estado.
5. Transferir a un asesor y comprobar que un mensaje posterior del mismo contacto no recibe respuesta automática.

La sesión dura dos horas. Después hay que volver a introducir el token.

## 7. Identificar una suspensión

- La página `/demo` no carga o tarda mucho en responder: probablemente el servicio está suspendido o reactivándose.
- En el panel de Render, el estado aparece como **Suspended** o el último despliegue como inactivo.
- Los logs no muestran actividad del worker mientras esté suspendido.

## 8. Reiniciar la demo tras una suspensión

1. Abrir `/demo` (o `/health`) para reactivar el servicio y esperar a que responda 200. Puede tardar varios segundos o más de 20 segundos.
2. Introducir el token en `/demo`.
3. Los datos de la base se conservan. Los eventos que llegaron durante la suspensión no se procesan hasta que el worker vuelve a arrancar.

No ejecutar una herramienta que haga ping para evitar la suspensión.

## 9. Detener o eliminar al terminar

- **Detener:** en el panel, **Suspend** o **Stop**. Deja de consumir recursos.
- **Eliminar:** borrar el servicio y la base de demostración. Esto es irreversible.
- Revocar `DEMO_TOKEN` cambiándolo en el panel. Cualquier sesión abierta caduca en dos horas.
- Si la base era de prueba, eliminarla para no conservar datos sintéticos innecesarios.

## Aislamiento entre visitantes

**Todos los visitantes autenticados ven el mismo conjunto de datos sintéticos.** No hay un espacio privado por visitante: es una decisión de diseño de la demostración. Por eso no debe contener datos reales, ni de clientes ni de la empresa. Quien tenga el token ve todo lo que hay en la base de demostración.

## Qué no debe hacer la demostración

- No activar `META_SEND_ENABLED`, `WHATSAPP_DRY_RUN=false`, `ANTHROPIC_ENABLED` ni `AGENT_DRY_RUN=false`.
- No prometer atención por WhatsApp 24/7.
- No usar datos reales de clientes.
- No compartir el token por canales no seguros.
- No añadir endpoints administrativos públicos.
