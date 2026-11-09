"""Adaptador de la API de Anthropic (SDK oficial). Único módulo del proyecto que importa el SDK.

Política de reintentos (solo donde la API no factura la generación):
  * Reintenta: 429 (límite de tasa) y 5xx/529 (sobrecarga), con espera exponencial y máximo configurable.
  * No reintenta: timeouts (la generación puede haberse facturado), errores de autenticación,
    errores de conexión (no se sabe si la solicitud llegó) ni respuestas inválidas.

Concurrencia: un semáforo limita las llamadas simultáneas; si no hay cupo dentro del tiempo límite,
se devuelve LLMBusyError sin llamar a la API.

Logs: solo identificadores, modelo, conteos de tokens y códigos de error. Nunca prompts ni respuestas.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import anthropic

from app.integrations.llm import (
    LLMAuthError,
    LLMBusyError,
    LLMDisabledError,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUsage,
)

logger = logging.getLogger("lacusine.agent.anthropic")
PROVIDER_NAME = "anthropic"
BACKOFF_BASE_SECONDS = 0.5


def worst_case_call_seconds(timeout_seconds: float, max_retries: int) -> float:
    """Peor duración de una llamada: espera de semáforo + intentos con timeout + backoff entre intentos."""
    attempts = max_retries + 1
    backoff = sum(BACKOFF_BASE_SECONDS * (2**i) for i in range(max_retries))
    return timeout_seconds * (attempts + 1) + backoff
# Valores según la documentación oficial de Anthropic (guía de migración de Sonnet 5.5):
#  * claude-sonnet-5-5 admite "adaptive" y "between_tools". "disabled" devuelve 400 en este modelo.
#  * "between_tools" no genera pensamiento inicial; sin herramientas la respuesta es solo texto.
#  * "between_tools" acepta esfuerzo low, medium y high; con xhigh o max devuelve 400.
#  * Otros modelos: solo "adaptive", como estaba verificado antes. "between_tools" NO se extiende a ellos.
THINKING_TYPES_BY_MODEL: dict[str, frozenset[str]] = {
    "claude-sonnet-5-5": frozenset({"adaptive", "between_tools"}),
}
DEFAULT_THINKING_TYPES: frozenset[str] = frozenset({"adaptive"})
BETWEEN_TOOLS_EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})
SUPPORTED_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


class AnthropicAgentClient:
    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 20.0,
        max_retries: int = 1,
        max_concurrency: int = 2,
        prompt_caching: bool = False,
        effort: str | None = None,
        thinking: str | None = None,
        sdk_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LLMDisabledError("missing_api_key")
        if not model:
            raise LLMDisabledError("missing_model")
        allowed_thinking = THINKING_TYPES_BY_MODEL.get(model, DEFAULT_THINKING_TYPES)
        if thinking is not None and thinking not in allowed_thinking:
            raise ValueError("thinking_type_unsupported_for_model")
        if effort is not None and effort not in SUPPORTED_EFFORT_LEVELS:
            raise ValueError("effort_unsupported")
        if thinking == "between_tools" and effort is not None and effort not in BETWEEN_TOOLS_EFFORT_LEVELS:
            raise ValueError("effort_incompatible_with_between_tools")
        self._effort = effort
        self._thinking = thinking
        if max_retries < 0 or max_retries > 3:
            raise ValueError("max_retries debe estar entre 0 y 3")
        if max_concurrency < 1:
            raise ValueError("max_concurrency debe ser al menos 1")
        self.model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._prompt_caching = prompt_caching
        self._sleep = sleep
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        # max_retries=0 en el SDK: la política de reintentos vive aquí, explícita y acotada.
        self._sdk = sdk_client or anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._semaphore.acquire(timeout=self._timeout):
            raise LLMBusyError()
        try:
            return self._with_retries(request)
        finally:
            self._semaphore.release()

    def _with_retries(self, request: LLMRequest) -> LLMResponse:
        attempt = 0
        while True:
            try:
                return self._call(request)
            except LLMError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                delay = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning("llm_retry call_id=%s code=%s attempt=%d", request.call_id, exc.code, attempt + 1)
                self._sleep(delay)
                attempt += 1

    def _call(self, request: LLMRequest) -> LLMResponse:
        system_block: dict[str, Any] = {"type": "text", "text": request.system}
        if self._prompt_caching:
            system_block["cache_control"] = {"type": "ephemeral"}
        # Parámetros opcionales: solo se envían si están configurados. Sin configuración la solicitud no cambia.
        optional: dict[str, Any] = {}
        if self._thinking is not None:
            optional["thinking"] = {"type": self._thinking}
        if self._effort is not None:
            optional["output_config"] = {"effort": self._effort}
        try:
            message = self._sdk.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=[system_block],
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                **optional,
            )
        except anthropic.AuthenticationError:
            raise LLMAuthError() from None
        except anthropic.RateLimitError:
            raise LLMRateLimitError() from None
        except anthropic.APITimeoutError:
            raise LLMTimeoutError() from None
        except anthropic.APIConnectionError:
            # No reintentable: no se sabe si la solicitud llegó a la API.
            raise LLMError("connection_failed") from None
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529 or exc.status_code >= 500:
                raise LLMUnavailableError() from None
            raise LLMError("api_status_error") from None

        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        usage = message.usage
        result = LLMResponse(
            text=text,
            model=self.model,
            provider=PROVIDER_NAME,
            usage=LLMUsage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
            ),
            truncated=message.stop_reason == "max_tokens",
            block_types=tuple(str(getattr(block, "type", "unknown")) for block in message.content),
        )
        logger.info(
            "llm_call_completed call_id=%s model=%s in=%d out=%d",
            request.call_id, self.model, result.usage.input_tokens, result.usage.output_tokens,
        )
        return result
