"""Endpoints del webhook de WhatsApp Cloud API.

El POST valida, persiste de forma idempotente y responde. No llama a Claude ni ejecuta trabajo costoso:
el procesamiento lo hará un worker sobre webhook_events en una fase posterior.
"""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.core.logging_utils import logger, mask_phone
from app.models.webhook import PayloadValidationError, extract_inbound_messages, extract_statuses
from app.repositories.errors import StorageError
from app.repositories.webhook_events import RegisterOutcome, WebhookEventRecord
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
    repository = request.app.state.webhook_repository
    if not settings.meta_app_secret or repository is None:
        # Falla cerrado: sin secreto no se valida el origen; sin base no se puede garantizar el registro.
        logger.error("webhook_not_configured")
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

    try:
        messages = extract_inbound_messages(payload, max_messages=settings.max_messages_per_request)
    except PayloadValidationError as exc:
        # El código de error describe la regla incumplida; nunca incluye el contenido recibido.
        logger.warning("webhook_payload_rejected reason=%s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

    accepted = 0
    duplicates = 0
    conflicts = 0
    for message in messages:
        record = WebhookEventRecord(
            external_event_id=message.message_id,
            contact_wa_id=message.sender,
            payload=message.raw,
            payload_sha256=message.payload_sha256,
            business_phone_id=message.business_phone_id,
        )
        try:
            outcome = repository.register(record)
        except StorageError as exc:
            # Respuesta no exitosa: Meta reintenta. Los reintentos son seguros porque el registro es idempotente.
            logger.error("webhook_storage_failed reason=%s", exc)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage_unavailable")

        if outcome is RegisterOutcome.NEW:
            accepted += 1
            logger.info("inbound_message_accepted sender=%s", mask_phone(message.sender))
        elif outcome is RegisterOutcome.DUPLICATE:
            duplicates += 1
            logger.info("inbound_message_duplicate")
        else:
            # Mismo wamid con contenido distinto: no se sobrescribe; queda registrado para revisión.
            conflicts += 1
            logger.warning("inbound_message_conflict")

    # Estados de entrega: se registran para correlacionarlos con el outbox. No cuentan en la respuesta de mensajes.
    try:
        statuses = extract_statuses(payload, max_items=settings.max_messages_per_request * 4)
    except PayloadValidationError as exc:
        logger.warning("webhook_payload_rejected reason=%s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")
    for event in statuses:
        record = WebhookEventRecord(
            external_event_id=event.external_event_id,
            contact_wa_id=None,
            payload=event.raw,
            payload_sha256=event.payload_sha256,
            event_type="status",
            business_phone_id=event.business_phone_id,
        )
        try:
            repository.register(record)
        except StorageError as exc:
            logger.error("webhook_storage_failed reason=%s", exc)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage_unavailable")

    return Response(
        content=json.dumps({"accepted": accepted, "duplicates": duplicates, "conflicts": conflicts}),
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )
