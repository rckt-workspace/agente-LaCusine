"""Transporte EXPERIMENTAL por WhatsApp Web (dispositivo vinculado mediante Baileys). NO es la API oficial de Meta.

Riesgo: el uso de WhatsApp Web no oficial puede provocar restricciones o suspensión de la cuenta. Este módulo no
modifica la cuenta ni garantiza cumplimiento de las condiciones de Meta. Está desactivado por defecto
(WHATSAPP_TRANSPORT=cloud_api) y las respuestas automáticas requieren WEB_BRIDGE_AUTO_REPLY=true.

Piezas:
  * Firma HMAC-SHA256 entre el puente Node y FastAPI, con ventana de 5 minutos contra repetición.
  * Conversión de un mensaje de WhatsApp Web al formato interno de Meta, con filtros: propios, grupos, historial,
    no texto y mensajes antiguos se rechazan antes de llegar al procesamiento.
  * WebBridgeSender: implementa el contrato del remitente del despachador. Sin respuestas automáticas, nunca envía.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any

import httpx

from app.integrations.whatsapp_client import (
    BusinessNumberMismatchError,
    ConnectionFailedError,
    MetaRejectedError,
    RecipientNotAllowedError,
    SendDisabledError,
    SendRequest,
    SendResult,
    UncertainSendError,
    WhatsAppError,
    validate_request,
)

SIGNATURE_WINDOW_SECONDS = 300
INBOUND_MAX_AGE_SECONDS = 120
MAX_CLOCK_SKEW_SECONDS = 60
WA_ID_PATTERN = re.compile(r"^[0-9]{6,20}$")
# Ruta opaca que el conector asigna a un chat @lid (12 dígitos, empieza por 9). No es un número telefónico.
OPAQUE_ROUTE_PATTERN = re.compile(r"^9[0-9]{11}$")
MAX_TEXT_CHARS = 4096
MAX_MESSAGE_ID_CHARS = 128


INBOUND_MODE_SINGLE = "single_chat"
INBOUND_MODE_ALL = "web_inbound_all"
INBOUND_MODE_PUBLIC = "public_inbound"
INBOUND_ALL_MAX_SECONDS = 900  # 15 minutos: ventana máxima del modo universal de la primera prueba
# Token exacto que debe fijar el operador para abrir la atención pública por WhatsApp Web. Sin él, no arranca.
PUBLIC_INBOUND_CONFIRMATION = "ACTIVAR_ENTRADA_PUBLICA_WEB_BRIDGE"


def inbound_window_state(mode: str, until: int | None, now: int | None = None) -> str:
    """'single_chat', 'public' (sin ventana: solo la confirmación del arranque), 'active', 'expired', 'missing' o
    'invalid'. Función pura."""
    if mode == INBOUND_MODE_SINGLE:
        return "single_chat"
    if mode == INBOUND_MODE_PUBLIC:
        return "public"
    if mode != INBOUND_MODE_ALL:
        return "invalid"
    if until is None:
        return "missing"
    current = int(time.time()) if now is None else now
    remaining = until - current
    if remaining <= 0:
        return "expired"
    if remaining > INBOUND_ALL_MAX_SECONDS:
        return "invalid"
    return "active"


class BridgeRejectedError(ValueError):
    """El mensaje del puente no es procesable. El código describe la regla; nunca incluye el contenido."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def sign(secret: str, direction: str, timestamp: str, body: bytes) -> str:
    message = f"{direction}.{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(secret: str, direction: str, timestamp: str, body: bytes, signature: str | None,
           now: int | None = None) -> bool:
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - stamp) > SIGNATURE_WINDOW_SECONDS:
        return False
    expected = sign(secret, direction, timestamp, body)
    return hmac.compare_digest(expected, signature or "")


def inbound_to_meta_payload(item: Any, line_id: str, now: int | None = None) -> dict[str, Any]:
    """Convierte un mensaje entrante de WhatsApp Web en el payload interno de Meta. Lanza BridgeRejectedError."""
    if not isinstance(item, dict):
        raise BridgeRejectedError("item_not_object")
    if item.get("from_me") is True:
        raise BridgeRejectedError("own_message")
    if item.get("is_group") is True:
        raise BridgeRejectedError("group_message")
    if item.get("is_history") is True:
        raise BridgeRejectedError("history_message")
    if item.get("kind") != "text":
        raise BridgeRejectedError("not_text")

    message_id = item.get("message_id")
    if not isinstance(message_id, str) or not 0 < len(message_id) <= MAX_MESSAGE_ID_CHARS:
        raise BridgeRejectedError("message_id_invalid")
    sender = item.get("from")
    if not isinstance(sender, str) or not WA_ID_PATTERN.match(sender):
        raise BridgeRejectedError("sender_invalid")
    timestamp = item.get("timestamp")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise BridgeRejectedError("timestamp_invalid")
    current = int(time.time()) if now is None else now
    if current - timestamp > INBOUND_MAX_AGE_SECONDS or timestamp - current > MAX_CLOCK_SKEW_SECONDS:
        # Mensajes de respaldo o antiguos: no abren conversaciones ni generan respuestas.
        raise BridgeRejectedError("message_too_old")
    text = item.get("text")
    if not isinstance(text, str) or not 0 < len(text) <= MAX_TEXT_CHARS:
        raise BridgeRejectedError("text_length")

    return {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": line_id},
                    "messages": [{
                        "from": sender,
                        "id": message_id,
                        "timestamp": str(timestamp),
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


class WebBridgeSender:
    """Remitente del despachador para el transporte web. Sin respuestas automáticas, nunca envía."""

    def __init__(self, *, url: str | None, secret: str | None, line_id: str | None, auto_reply: bool,
                 allowed_recipients: tuple[str, ...] = (), audience_mode: str = "test_allowlist",
                 inbound_mode: str = INBOUND_MODE_SINGLE, inbound_until: int | None = None,
                 timeout_seconds: float = 10.0, client: httpx.Client | None = None) -> None:
        self._url = (url or "").rstrip("/")
        self._secret = secret or ""
        self._line_id = line_id or ""
        self._auto_reply = auto_reply
        self._allowed = frozenset(allowed_recipients)
        self._audience = audience_mode
        self._inbound_mode = inbound_mode
        self._inbound_until = inbound_until
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @property
    def enabled(self) -> bool:
        if not (self._auto_reply and self._url and self._secret and self._line_id):
            return False
        return inbound_window_state(self._inbound_mode, self._inbound_until) in ("single_chat", "active", "public")

    def _authorized_route(self, route: str) -> bool:
        """Un destinatario está autorizado si es un número listado o si es una ruta opaca cuya identidad verificada
        (número de WhatsApp, resuelta por el conector) está listada. Cualquier fallo de resolución = no autorizado."""
        if route in self._allowed:
            return True
        if not OPAQUE_ROUTE_PATTERN.match(route):
            return False
        phone = self._verified_phone(route)
        return phone is not None and phone in self._allowed

    def _route_known(self, route: str) -> bool:
        """Pregunta firmada al conector: ¿esta ruta nació de un mensaje entrante suyo? Cualquier fallo = no."""
        if not WA_ID_PATTERN.match(route):
            return False
        body = json.dumps({"route": route}, separators=(",", ":")).encode("utf-8")
        stamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Bridge-Timestamp": stamp,
            "X-Bridge-Signature": sign(self._secret, "status", stamp, body),
        }
        try:
            response = self._client.post(f"{self._url}/route", content=body, headers=headers)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            return response.json().get("known") is True
        except ValueError:
            return False

    def _verified_phone(self, route: str) -> str | None:
        body = json.dumps({"route": route}, separators=(",", ":")).encode("utf-8")
        stamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Bridge-Timestamp": stamp,
            "X-Bridge-Signature": sign(self._secret, "status", stamp, body),
        }
        try:
            response = self._client.post(f"{self._url}/identity", content=body, headers=headers)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            phone = response.json().get("phone")
        except ValueError:
            return None
        return phone if isinstance(phone, str) and WA_ID_PATTERN.match(phone) else None

    def send_text(self, request: SendRequest) -> SendResult:
        if not self.enabled:
            # Incluye la caducidad de la ventana universal: pasado el plazo no se envía nada.
            raise SendDisabledError()
        validate_request(request)
        if request.business_phone_id != self._line_id:
            raise BusinessNumberMismatchError()
        universal = self._inbound_mode == INBOUND_MODE_ALL
        if self._inbound_mode == INBOUND_MODE_PUBLIC:
            # Atención pública: solo a rutas que el conector confirma como originadas por un evento entrante.
            # Nunca a un destinatario arbitrario. Sin confirmación, no se envía.
            if not self._route_known(request.to):
                raise RecipientNotAllowedError()
        elif not universal and self._audience == "test_allowlist" and not self._authorized_route(request.to):
            # Modo de prueba de un solo chat: nunca se responde fuera de la lista.
            raise RecipientNotAllowedError()

        body = json.dumps({"to": request.to, "text": request.text, "idempotency_key": request.idempotency_key},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        stamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Bridge-Timestamp": stamp,
            "X-Bridge-Signature": sign(self._secret, "send", stamp, body),
        }
        try:
            response = self._client.post(f"{self._url}/send", content=body, headers=headers)
        except httpx.ConnectError:
            # No se estableció conexión: el puente no recibió nada. Reintento seguro.
            raise ConnectionFailedError() from None
        except httpx.HTTPError:
            # Hubo escritura o lectura: el puente pudo haber enviado. Nunca se reintenta automáticamente.
            raise UncertainSendError() from None

        if response.status_code == 200:
            try:
                message_id = response.json().get("message_id")
            except ValueError:
                raise UncertainSendError() from None
            if not isinstance(message_id, str) or not message_id:
                raise UncertainSendError()
            return SendResult(message_id=message_id, http_status=200)
        if response.status_code == 409:
            # El puente rechazó enviar (apagado, desconectado o kill switch) antes de tocar WhatsApp.
            raise SendDisabledError()
        if response.status_code >= 500:
            raise UncertainSendError()
        raise MetaRejectedError("bridge_rejected")

    def show_typing(self, to: str, inbound_message_id: str) -> None:
        """«Escribiendo…» mientras el agente prepara la respuesta. Señal firmada al conector, que la caduca sola
        (máximo 60 s) y la apaga al enviar. Solo para destinatarios que podrían recibir respuesta."""
        if not self.enabled:
            raise SendDisabledError()
        if not self._may_reply(to):
            raise RecipientNotAllowedError()
        body = json.dumps({"route": to, "action": "start"}, separators=(",", ":")).encode("utf-8")
        stamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Bridge-Timestamp": stamp,
            "X-Bridge-Signature": sign(self._secret, "send", stamp, body),
        }
        try:
            response = self._client.post(f"{self._url}/typing", content=body, headers=headers)
        except httpx.HTTPError:
            raise WhatsAppError("typing_transport") from None
        if response.status_code != 200:
            raise WhatsAppError("typing_rejected")

    def _may_reply(self, to: str) -> bool:
        """Misma autorización que el envío, sin enviar nada."""
        if self._inbound_mode == INBOUND_MODE_PUBLIC:
            return self._route_known(to)
        if self._inbound_mode == INBOUND_MODE_ALL:
            return True
        if self._audience == "test_allowlist":
            return self._authorized_route(to)
        return True
