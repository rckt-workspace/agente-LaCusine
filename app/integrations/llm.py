"""Interfaz de proveedor de lenguaje. El agente depende de esta interfaz, no del SDK de Anthropic."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    call_id: str
    system: str
    messages: tuple[ChatMessage, ...]
    max_output_tokens: int
    purpose: str = "commercial_reply"


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: LLMUsage
    truncated: bool = False


class LLMError(Exception):
    """Error del proveedor. Solo expone un código; nunca el texto original, que puede contener datos.

    billed_state indica si la llamada pudo haberse facturado:
      * "not_billed": la API garantiza que no generó cobro (rechazo previo a la generación).
      * "uncertain": no se sabe (valor por defecto y el más conservador).
    """

    code = "provider_error"
    retryable = False
    billed_state = "uncertain"

    def __init__(self, code: str | None = None) -> None:
        super().__init__(code or self.code)
        self.code = code or self.code


class LLMDisabledError(LLMError):
    code = "disabled"
    billed_state = "not_billed"


class LLMAuthError(LLMError):
    code = "auth_failed"
    billed_state = "not_billed"


class LLMRateLimitError(LLMError):
    code = "rate_limited"
    retryable = True
    billed_state = "not_billed"


class LLMUnavailableError(LLMError):
    code = "unavailable"
    retryable = True


class LLMTimeoutError(LLMError):
    # No se reintenta: una generación interrumpida puede haber sido facturada.
    code = "timeout"


class LLMBusyError(LLMError):
    code = "busy"
    billed_state = "not_billed"


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass
class FakeLLMProvider:
    """Proveedor simulado para pruebas y modo dry-run. No hace llamadas de red ni cobra nada.

    `script` puede contener textos (respuesta), excepciones LLMError (fallo) o funciones que reciben la
    solicitud y devuelven texto. Se consume en orden; al agotarse, responde con `default_text`.
    """

    script: list[object] = field(default_factory=list)
    default_text: str = '{"reply_text": "Gracias por escribirnos.", "intent": "other", "lead_stage": "NEW", "recommended_product_ids": [], "requires_human": false, "handoff_reason": null}'
    model: str = "fake-model"
    name: str = "fake"
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(input_tokens=500, output_tokens=120))
    calls: list[LLMRequest] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self.calls.append(request)
            item = self.script.pop(0) if self.script else self.default_text
        if isinstance(item, LLMError):
            raise item
        text = item(request) if callable(item) else str(item)
        return LLMResponse(text=text, model=self.model, provider=self.name, usage=self.usage)
