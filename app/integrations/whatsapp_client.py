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
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import httpx

logger = logging.getLogger("lacusine.whatsapp")

GRAPH_HOST = "https://graph.facebook.com"
MAX_TEXT_CHARS = 4096
WA_ID_PATTERN = re.compile(r"^[0-9]{6,20}$")
PHONE_ID_PATTERN = re.compile(r"^[0-9]{6,32}$")
TYPING_MESSAGE_ID_PATTERN = re.compile(r"^wamid\.[A-Za-z0-9+/=_.-]{1,200}$")


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
    """La solicitud no salió del proceso (conexión no establecida o sin conexión disponible). Reintento seguro.

    Solo se usa para errores que ocurren ANTES de enviar datos. Un error de lectura o de escritura después
    de iniciar la solicitud es "uncertain", aunque sea un error de transporte.
    """

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
        audience_mode: str = "test_allowlist",
    ) -> None:
        if audience_mode not in ("test_allowlist", "public"):
            raise SendConfigError("audience_mode_unknown")
        self._audience = audience_mode
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
            if audience_mode == "test_allowlist" and not allowed_recipients:
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
        if self._audience == "test_allowlist" and request.to not in self._allowed:
            raise RecipientNotAllowedError()
        if request.business_phone_id != self._phone_number_id:
            raise BusinessNumberMismatchError()
        if not self._semaphore.acquire(timeout=self._timeout):
            raise BusyError()
        try:
            return self._post(request)
        finally:
            self._semaphore.release()

    def show_typing(self, to: str, inbound_message_id: str) -> None:
        """Marca como leído el mensaje recibido y muestra 'escribiendo...' (API oficial de Meta).

        Solo con envío habilitado y destinatario permitido. Cualquier fallo lanza WhatsAppError: quien llama
        lo ignora, porque el indicador nunca debe impedir la respuesta comercial.
        """
        if not self._enabled:
            raise SendDisabledError()
        if not WA_ID_PATTERN.match(to or ""):
            raise PayloadRejectedError("recipient_format")
        if self._audience == "test_allowlist" and to not in self._allowed:
            raise RecipientNotAllowedError()
        if not TYPING_MESSAGE_ID_PATTERN.match(inbound_message_id or ""):
            raise PayloadRejectedError("typing_message_id")
        if not self._semaphore.acquire(timeout=self._timeout):
            raise BusyError()
        try:
            url = f"{GRAPH_HOST}/{self._version}/{self._phone_number_id}/messages"
            body = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": inbound_message_id,
                "typing_indicator": {"type": "text"},
            }
            headers = {"Authorization": f"Bearer {self._token}"}
            try:
                response = self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError:
                raise WhatsAppError("typing_transport") from None
            if response.status_code != 200:
                info = parse_meta_error(response)
                _log_rejection("typing", info)
                raise MetaRejectedError(f"typing_{info.specific_code()}")
            logger.info("whatsapp_typing_accepted http=%d", response.status_code)
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
        except httpx.PoolTimeout:
            # No hubo conexión disponible: la solicitud nunca salió del proceso.
            raise ConnectionFailedError("pool_timeout") from None
        except httpx.ConnectTimeout:
            # El intento de conexión no se completó: la solicitud no llegó a enviarse.
            raise ConnectionFailedError("connect_timeout") from None
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
            raise RateLimitedError("http_429")
        info = parse_meta_error(response)
        if status in (401, 403):
            # Token caducado o revocado (p. ej. código 190). Se registra el código de Meta, nunca el token.
            _log_rejection("send", info)
            logger.error("meta_auth_rejected http=%d hint=renovar_META_ACCESS_TOKEN", status)
            raise AuthRejectedError(info.specific_code())
        if status >= 500:
            _log_rejection("send", info)
            raise UncertainSendError(f"http_{status}")
        _log_rejection("send", info)
        if info.code in RETRYABLE_META_CODES:
            raise RateLimitedError(info.specific_code())
        raise MetaRejectedError(info.specific_code())


# Códigos de Meta que la documentación describe como transitorios: se reintentan con backoff.
# 130429 (límite de rendimiento), 131056 (demasiados mensajes al mismo número en poco tiempo), 80007 ("try again later").
RETRYABLE_META_CODES = frozenset({130429, 131056, 80007})
_DETAILS_MAX_CHARS = 160
_BEARER_OR_TOKEN = re.compile(r"(Bearer\s+\S+|EAA[A-Za-z0-9_-]{10,})")
_LONG_DIGITS = re.compile(r"\d{7,}")


@dataclass(frozen=True)
class MetaErrorInfo:
    """Error de Meta sin datos sensibles: nunca incluye el token, el cuerpo del mensaje ni números completos."""

    http_status: int
    code: int | None
    error_type: str | None
    subcode: int | None
    details: str | None
    fbtrace_id: str | None

    def specific_code(self) -> str:
        """Código interno que conserva el identificador específico de Meta (no se sustituye por http_400)."""
        return f"meta_{self.code}" if self.code is not None else f"http_{self.http_status}"


def redact_details(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = _BEARER_OR_TOKEN.sub("[redactado]", text)
    cleaned = _LONG_DIGITS.sub("[numero]", cleaned)
    return cleaned[:_DETAILS_MAX_CHARS]


def parse_meta_error(response: httpx.Response) -> MetaErrorInfo:
    """Extrae response.json()["error"]. Si el cuerpo no es JSON o falta el error, devuelve campos vacíos."""
    try:
        data = response.json()
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        error = {}
    error_data = error.get("error_data") if isinstance(error.get("error_data"), dict) else {}
    details = error_data.get("details") or error.get("message")
    return MetaErrorInfo(
        http_status=response.status_code,
        code=_int_or_none(error.get("code")),
        error_type=_text_or_none(error.get("type")),
        subcode=_int_or_none(error.get("error_subcode")),
        details=redact_details(details if isinstance(details, str) else None),
        fbtrace_id=_text_or_none(error.get("fbtrace_id")),
    )


def _log_rejection(operation: str, info: MetaErrorInfo) -> None:
    logger.warning(
        "meta_api_rejected operation=%s http=%d code=%s type=%s subcode=%s fbtrace_id=%s details=%s",
        operation, info.http_status, info.code if info.code is not None else "-",
        info.error_type or "-", info.subcode if info.subcode is not None else "-",
        info.fbtrace_id or "-", info.details or "-",
    )


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _text_or_none(value) -> str | None:
    return value[:80] if isinstance(value, str) and value else None


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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def send_text(self, request: SendRequest) -> SendResult:
        validate_request(request)
        with self._lock:
            self.sent.append(request)
            item = self.script.pop(0) if self.script else None
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request)
        # Identificador único por envío, como en Meta. Un contador por proceso colisiona entre procesos.
        return SendResult(message_id=f"wamid.SIM{uuid.uuid4().hex}", http_status=200)
