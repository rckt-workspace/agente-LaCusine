"""Adaptador de salida de WhatsApp Cloud API (Graph API) y su simulador.

Cada error declara su `outcome`:
  "not_sent"  -> la solicitud no salió o Meta la rechazó antes de aceptarla. Reintento seguro.
  "uncertain" -> Meta pudo haber aceptado el mensaje (timeout de lectura, 5xx, respuesta sin ID).
                 Nunca se reintenta automáticamente: pasa a conciliación.

Seguridad:
  * Por defecto NO envía: META_SEND_ENABLED=false o WHATSAPP_DRY_RUN=true bloquean cualquier solicitud.
  * El token viaja solo en la cabecera Authorization y nunca aparece en logs, repr ni errores.
  * Solo se envía a destinatarios de WHATSAPP_ALLOWED_RECIPIENTS (lista no vacía si el envío está activo).
  * El número de origen debe coincidir con META_PHONE_NUMBER_ID.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Protocol

import httpx

logger = logging.getLogger("lacusine.whatsapp")

GRAPH_HOST = "https://graph.facebook.com"
MAX_TEXT_CHARS = 4096
WA_ID_PATTERN = re.compile(r"^[0-9]{6,20}$")
PHONE_ID_PATTERN = re.compile(r"^[0-9]{6,32}$")


class WhatsAppError(Exception):
    code = "whatsapp_error"
    outcome = "not_sent"
    retryable = False

    def __init__(self, code: str | None = None) -> None:
        super().__init__(code or self.code)
        self.code = code or self.code


class SendDisabledError(WhatsAppError):
    code = "send_disabled"


class SendConfigError(WhatsAppError):
    code = "send_config_invalid"


class RecipientNotAllowedError(WhatsAppError):
    code = "recipient_not_allowed"


class BusinessNumberMismatchError(WhatsAppError):
    code = "business_number_mismatch"


class PayloadRejectedError(WhatsAppError):
    code = "payload_rejected"


class RateLimitedError(WhatsAppError):
    code = "rate_limited"
    retryable = True


class AuthRejectedError(WhatsAppError):
    code = "auth_rejected"


class MetaRejectedError(WhatsAppError):
    code = "meta_rejected"


class ConnectionFailedError(WhatsAppError):
    """La conexión no se estableció: la solicitud no salió. Reintento seguro."""

    code = "connection_failed"
    retryable = True


class BusyError(WhatsAppError):
    code = "busy"
    retryable = True


class UncertainSendError(WhatsAppError):
    """Meta pudo haber aceptado el mensaje. No se reintenta automáticamente."""

    code = "send_uncertain"
    outcome = "uncertain"


@dataclass(frozen=True)
class SendRequest:
    to: str
    text: str
    business_phone_id: str
    idempotency_key: str


@dataclass(frozen=True)
class SendResult:
    message_id: str
    http_status: int


class WhatsAppSender(Protocol):
    def send_text(self, request: SendRequest) -> SendResult: ...


def validate_request(request: SendRequest) -> None:
    if not WA_ID_PATTERN.match(request.to):
        raise PayloadRejectedError("recipient_format")
    if not request.text or len(request.text) > MAX_TEXT_CHARS:
        raise PayloadRejectedError("text_length")


class CloudApiSender:
    def __init__(
        self,
        *,
        access_token: str | None,
        phone_number_id: str | None,
        graph_api_version: str | None,
        allowed_recipients: tuple[str, ...],
        send_enabled: bool,
        dry_run: bool,
        timeout_seconds: float = 10.0,
        max_concurrency: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        missing = [
            name for name, value in (
                ("META_ACCESS_TOKEN", access_token),
                ("META_PHONE_NUMBER_ID", phone_number_id),
                ("META_GRAPH_API_VERSION", graph_api_version),
            ) if not value
        ]
        if send_enabled and not dry_run:
            if missing:
                raise SendConfigError("missing:" + ",".join(missing))
            if not allowed_recipients:
                raise SendConfigError("recipients_allowlist_empty")
            if not PHONE_ID_PATTERN.match(phone_number_id or ""):
                raise SendConfigError("phone_number_id_format")
        self._token = access_token
        self._phone_number_id = phone_number_id
        self._version = graph_api_version
        self._allowed = frozenset(allowed_recipients)
        self._enabled = send_enabled and not dry_run
        self._semaphore = threading.BoundedSemaphore(max(1, max_concurrency))
        self._timeout = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send_text(self, request: SendRequest) -> SendResult:
        if not self._enabled:
            raise SendDisabledError()
        validate_request(request)
        if request.to not in self._allowed:
            raise RecipientNotAllowedError()
        if request.business_phone_id != self._phone_number_id:
            raise BusinessNumberMismatchError()
        if not self._semaphore.acquire(timeout=self._timeout):
            raise BusyError()
        try:
            return self._post(request)
        finally:
            self._semaphore.release()

    def _post(self, request: SendRequest) -> SendResult:
        url = f"{GRAPH_HOST}/{self._version}/{self._phone_number_id}/messages"
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": request.to,
            "type": "text",
            "text": {"body": request.text, "preview_url": False},
        }
        # La idempotencia del envío la garantiza la base (clave única del outbox), no Meta.
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            response = self._client.post(url, json=body, headers=headers)
        except httpx.ConnectError:
            # La conexión no se estableció: la solicitud no salió.
            raise ConnectionFailedError() from None
        except httpx.TimeoutException:
            # Tras enviar la solicitud, un timeout no dice si Meta la procesó.
            raise UncertainSendError("timeout") from None
        except httpx.HTTPError:
            raise UncertainSendError("transport_error") from None

        status = response.status_code
        if status in (200, 201):
            message_id = _message_id(response)
            if message_id is None:
                raise UncertainSendError("response_without_message_id")
            logger.info("whatsapp_send_accepted http=%d", status)
            return SendResult(message_id=message_id, http_status=status)
        if status == 429:
            raise RateLimitedError()
        if status in (401, 403):
            raise AuthRejectedError()
        if status >= 500:
            raise UncertainSendError(f"http_{status}")
        if status == 400:
            raise MetaRejectedError("http_400")
        raise MetaRejectedError(f"http_{status}")


def _message_id(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        return None
    identifier = messages[0].get("id")
    return identifier if isinstance(identifier, str) and 0 < len(identifier) <= 128 else None


@dataclass
class SimulatedWhatsAppSender:
    """Simulador HTTP-equivalente para pruebas. No realiza ninguna conexión."""

    script: list = field(default_factory=list)
    sent: list[SendRequest] = field(default_factory=list)
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def send_text(self, request: SendRequest) -> SendResult:
        validate_request(request)
        with self._lock:
            self.sent.append(request)
            item = self.script.pop(0) if self.script else None
            self._counter += 1
            counter = self._counter
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request)
        return SendResult(message_id=f"wamid.SIM{counter:06d}", http_status=200)
