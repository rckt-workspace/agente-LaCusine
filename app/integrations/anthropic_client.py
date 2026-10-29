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
        sdk_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LLMDisabledError("missing_api_key")
        if not model:
            raise LLMDisabledError("missing_model")
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
        try:
            message = self._sdk.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=[system_block],
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
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
        )
        logger.info(
            "llm_call_completed call_id=%s model=%s in=%d out=%d",
            request.call_id, self.model, result.usage.input_tokens, result.usage.output_tokens,
        )
        return result
