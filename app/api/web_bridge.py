"""Entrada interna desde el conector WhatsApp Web (experimental, no oficial).

Solo existe cuando WHATSAPP_TRANSPORT=web_bridge y WEB_BRIDGE_AUTO_REPLY=true. En cualquier otro caso responde 404
para no revelar la ruta. Cada solicitud va firmada con WEB_BRIDGE_SECRET; los mensajes pasan los mismos filtros
y el mismo registro idempotente que los de Meta.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status

from app.api.webhook import read_limited_body, register_inbound_messages
from app.core.logging_utils import logger
from app.integrations.web_bridge import BridgeRejectedError, inbound_to_meta_payload, inbound_window_state, verify
from app.models.webhook import PayloadValidationError, extract_inbound_messages

router = APIRouter(prefix="/internal/web-bridge", tags=["web-bridge"])
MAX_ITEMS_PER_REQUEST = 20


def _correlation(item) -> str:
    """Identificador de correlación del conector: alfanumérico y corto. Nunca contiene texto ni números completos."""
    raw = item.get("correlation_id") if isinstance(item, dict) else None
    if not isinstance(raw, str):
        return "-"
    return "".join(ch for ch in raw if ch.isalnum())[:16] or "-"


@router.post("/inbound")
async def receive_web_inbound(request: Request) -> dict:
    settings = request.app.state.settings
    if settings.whatsapp_transport != "web_bridge" or not settings.web_bridge_auto_reply:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    repository = request.app.state.webhook_repository
    if not settings.web_bridge_secret or not settings.web_bridge_line_id or repository is None:
        logger.error("web_bridge_not_configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not_configured")

    raw_body = await read_limited_body(request, settings.max_body_bytes)
    stamp = request.headers.get("X-Bridge-Timestamp", "")
    signature = request.headers.get("X-Bridge-Signature")
    if not verify(settings.web_bridge_secret, "inbound", stamp, raw_body, signature):
        logger.warning("web_bridge_signature_rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_signature")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_json")
    items = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(items, list) or len(items) > MAX_ITEMS_PER_REQUEST:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

    window = inbound_window_state(settings.web_bridge_inbound_mode, settings.web_bridge_inbound_until)
    if window in ("expired", "missing", "invalid"):
        # Ventana universal cerrada o mal configurada: no se registra ningún mensaje nuevo.
        logger.info("web_bridge_inbound_refused reason=%s", window)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="inbound_window_closed")
    if window == "public" and settings.whatsapp_audience_mode != "public_inbound":
        # La atención pública exige audiencia public_inbound declarada; cualquier otra combinación se rechaza.
        logger.info("web_bridge_inbound_refused reason=public_not_declared")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="inbound_mode_not_public")

    line = settings.web_bridge_line_id
    allowed = set(settings.whatsapp_allowed_recipients)
    # Solo el modo de un chat aplica la lista de destinatarios. En web_inbound_all la protección es la del conector.
    test_mode = window == "single_chat" and settings.whatsapp_audience_mode == "test_allowlist"
    accepted_payloads: list[tuple[str, dict]] = []
    # Resultado por elemento (campo "items"): un descartado nunca aparece como aceptado en el conector.
    results: list[dict] = []
    rejected = 0

    def _reject(cid: str, reason: str) -> None:
        nonlocal rejected
        rejected += 1
        results.append({"correlation_id": cid, "outcome": "rejected", "reason": reason})

    for item in items:
        cid = _correlation(item)
        if test_mode and isinstance(item, dict):
            # La autorización compara la IDENTIDAD verificada por el conector (número de WhatsApp), no la ruta de
            # respuesta: un chat @lid tiene una ruta opaca que nunca coincide con un número permitido.
            identity = item.get("identity")
            if not isinstance(identity, str) or not identity:
                logger.info("stage=03_inbound_rejected cid=%s reason=identity_missing", cid)
                _reject(cid, "identity_missing")
                continue
            if identity not in allowed:
                # Modo de prueba: los desconocidos no entran al procesamiento ni consumen presupuesto de IA.
                logger.info("stage=03_inbound_rejected cid=%s reason=not_in_test_allowlist", cid)
                _reject(cid, "not_in_test_allowlist")
                continue
        try:
            accepted_payloads.append((cid, inbound_to_meta_payload(item, line)))
            logger.info("stage=03_inbound_validated cid=%s", cid)
        except BridgeRejectedError as exc:
            # El código describe la regla incumplida; nunca se registra el contenido del mensaje.
            logger.info("stage=03_inbound_rejected cid=%s reason=%s", cid, exc.code)
            _reject(cid, exc.code)

    messages_by_cid: list[tuple[str, object]] = []
    for cid, payload in accepted_payloads:
        try:
            messages_by_cid.extend((cid, message) for message in
                                   extract_inbound_messages(payload, max_messages=MAX_ITEMS_PER_REQUEST))
        except PayloadValidationError as exc:
            logger.info("web_bridge_message_rejected reason=%s", exc)
            _reject(cid, "payload_invalid")

    outcomes: list[str] = []
    accepted, duplicates, conflicts = register_inbound_messages(
        [message for _, message in messages_by_cid], repository, outcomes=outcomes)
    for (cid, _message), outcome in zip(messages_by_cid, outcomes):
        results.append({"correlation_id": cid, "outcome": outcome, "reason": None})
    logger.info("stage=06_webhook_persisted accepted=%d duplicates=%d conflicts=%d", accepted, duplicates, conflicts)
    return {"accepted": accepted, "duplicates": duplicates, "conflicts": conflicts, "rejected": rejected,
            "items": results}
