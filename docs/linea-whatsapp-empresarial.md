# Línea de WhatsApp empresarial para La Cuisine (atención pública)

Estado: **código preparado y probado; sin tráfico público; línea empresarial pendiente de registro.**

## 1. Por qué el número actual no sirve al público

La línea configurada hoy muestra `account_mode=LIVE`, `VERIFIED`, `CONNECTED`, pero su número visible termina en **2419**: es un número virtual de prueba de Meta (+1 555). Esos números admiten un número limitado de destinatarios registrados. Ninguna variable de este proyecto cambia esa restricción.

Por eso el preflight ya no basa la decisión en los tres estados. Una línea solo puede atender al público si cumple **todo** lo siguiente (`app/integrations/whatsapp_line.py`):

| Criterio | Motivo de rechazo |
|---|---|
| `account_mode = LIVE` | `cuenta_no_live` |
| `code_verification_status = VERIFIED` | `numero_no_verificado` |
| `status = CONNECTED` | `linea_no_conectada` |
| Número visible conocido | `numero_visible_desconocido` |
| Número visible que no sea de prueba (no empieza por +1 555) | `numero_de_prueba_de_meta` |
| Número visible igual a `WHATSAPP_EXPECTED_DISPLAY_NUMBER` | `numero_distinto_del_esperado` / `numero_esperado_no_configurado` |

## 2. Qué cambia en el código

- **Modo `test_allowlist`** (desarrollo): sin cambios. Exige `WHATSAPP_ALLOWED_RECIPIENTS` y no declara nada sobre atención pública. El preflight dice explícitamente *"NO es atención pública"*.
- **Modo `public`**:
  - No exige ni usa `WHATSAPP_ALLOWED_RECIPIENTS`.
  - Exige `WHATSAPP_EXPECTED_DISPLAY_NUMBER`, y rechaza cualquier número de prueba de Meta (`public_mode_refuses_meta_test_number`). El arranque del worker se detiene si no se cumple.
  - Mantiene la confirmación explícita `WHATSAPP_PUBLIC_CONFIRMATION`, la firma del webhook, la validación de la línea, el presupuesto, el kill switch y la transferencia humana.
- **Preflight** (`scripts/pilot_preflight.py`): solo lectura. Muestra el número visible con máscara (`***NNNN`), los motivos de rechazo y la decisión `linea_apta_para_publico=si|no`. El número completo nunca se imprime.
- Se eliminó código muerto que había tras un `return` en `check_meta_token`.

## 3. Garantías del envío en modo público (verificadas con cliente real y transporte simulado)

`tests/worker/test_public_mode_dispatch.py` cuenta las llamadas HTTP que saldrían hacia Meta:

| Caso | Llamadas a Meta |
|---|---|
| Cliente que escribió hace 1 h, sin lista | 1 (respuesta enviada) |
| Cliente fuera de la ventana de 24 h (25 h y 48 h) | 0 (`outside_service_window`) |
| Conversación iniciada por la empresa (sin mensaje entrante) | 0 |
| Conversación bajo control humano | 0 (`human_control`, cancelada) |
| Línea distinta de la registrada | 0 (`business_number_mismatch`) |
| `test_allowlist`, cliente que no está en la lista | 0 (`recipient_not_allowed`) |
| `test_allowlist`, cliente listado | 1 |

Además, el sistema no tiene ningún camino de envío iniciado por la empresa: el único envío de texto es el despachador de respuestas, y las notificaciones a asesores están simuladas.

## 4. Pasos para registrar la línea empresarial (fuera del código)

Verificar siempre con la documentación oficial vigente de Meta antes de ejecutar; las condiciones de Meta cambian.

1. **Elegir el número.** Debe ser una línea que no esté en uso en la app de WhatsApp (personal o Business). Ver la advertencia de la sección 5.
2. **Registrarlo en WhatsApp Cloud API** desde Meta Business Manager: cuenta de WhatsApp Business → número de teléfono → añadir. La verificación se hace por SMS o llamada al número.
3. **Obtener el identificador del número** (`META_PHONE_NUMBER_ID`) desde el panel de la API de WhatsApp. No es un secreto, pero identifica la línea.
4. **Token permanente** de usuario de sistema con permiso de mensajería de WhatsApp para esa cuenta. Se guarda solo en `.env` (`META_ACCESS_TOKEN`), nunca en el repositorio.
5. **Actualizar `.env`** (lo hace el propietario, no el código):
   ```
   META_PHONE_NUMBER_ID=<identificador de la línea registrada>
   WHATSAPP_EXPECTED_DISPLAY_NUMBER=+573153863179
   ```
6. **Ejecutar el preflight** en solo lectura y comprobar `linea_apta_para_publico=si` y `numero_visible=***3179`:
   ```powershell
   $env:LACUSINE_LOAD_DOTENV="true"; .\.venv\Scripts\python.exe scripts\pilot_preflight.py --expect-model claude-sonnet-5-5 --skip-db
   ```
7. **Solo después**, con autorización explícita: `WHATSAPP_AUDIENCE_MODE=public`, `WHATSAPP_PUBLIC_CONFIRMATION=ABRIR_ATENCION_PUBLICA_CONFIRMADA`, presupuestos prudentes y un único worker.

## 5. Advertencia crítica sobre el número +57 315 386 3179

Los últimos cuatro dígitos (3179) coinciden con el número personal de WhatsApp que se mencionó antes para mantener **independiente**. Si ese número es el personal, registrarlo en WhatsApp Cloud API **desvincula la cuenta personal** de la app de WhatsApp: hay que migrar o eliminar esa cuenta y se pierden sus chats. **Confirmar antes de registrar.** Si se quiere conservar el personal, usar otra línea dedicada.

## 6. Qué no hace este cambio

- No activa tráfico público ni envíos reales.
- No cambia el número de Meta, secretos, datos de producción ni migraciones.
- No garantiza que Meta acepte la línea: eso depende del registro y de las políticas de WhatsApp Business.
- Las respuestas fuera de la ventana de 24 horas requieren plantillas aprobadas, que **no** están implementadas. Mientras tanto, el sistema las bloquea (`outside_service_window`).
