"""Endpoints del webhook de WhatsApp Cloud API. Responden rápido: ninguna tarea costosa se ejecuta aquí."""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.core.logging_utils import logger, mask_phone
from app.models.webhook import extract_inbound_messages
from app.security.signature import verify_meta_signature

router = APIRouter(prefix="/webhook", tags=["whatsapp"])


@router.get("")
def verify_webhook(request: Request) -> PlainTextResponse:
    settings = request.app.state.settings
    params = request.query_params
    expected_token = settings.meta_verify_token
    if (
        not expected_token
        or params.get("hub.mode") != "subscribe"
        or not hmac.compare_digest(params.get("hub.verify_token", ""), expected_token)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="verification_failed")
    challenge = params.get("hub.challenge", "")
    return PlainTextResponse(content=challenge, status_code=status.HTTP_200_OK)


@router.post("")
async def receive_webhook(request: Request) -> Response:
    settings = request.app.state.settings
    if not settings.meta_app_secret:
        # Falla cerrado: sin secreto no se puede validar el origen.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not_configured")

    raw_body = await request.body()
    if len(raw_body) > settings.max_body_bytes:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="payload_too_large")

    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(settings.meta_app_secret, raw_body, signature):
        logger.warning("webhook_signature_rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_json")

    store = request.app.state.event_store
    accepted = 0
    duplicates = 0
    for message in extract_inbound_messages(payload):
        if store.register(message.message_id):
            accepted += 1
            # Fase 5: aquí se encolará el trabajo (cola de procesamiento). Sin tareas costosas en el webhook.
            logger.info("inbound_message_accepted sender=%s", mask_phone(message.sender))
        else:
            duplicates += 1
            logger.info("inbound_message_duplicate")

    return Response(
        content=json.dumps({"accepted": accepted, "duplicates": duplicates}),
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )
