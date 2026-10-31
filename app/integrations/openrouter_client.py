"""Adaptador de OpenRouter (Chat Completions, formato OpenAI). Implementa LLMProvider.

Endpoint oficial fijo: la URL no es configurable, para que la clave nunca se envíe a otro host.

Política de reintentos (solo donde la API garantiza que no hubo generación):
  * Reintenta: 429 (límite de tasa), hasta max_retries veces, con espera exponencial.
  * No reintenta ni se considera no facturado: timeouts, conexiones interrumpidas, 5xx y respuestas
    inválidas. En esos casos la llamada puede haber sido facturada: se marca como incierta y el caso
    va a conciliación. Nunca se reenvía sola.
  * No reintenta: 401/403 (autenticación), 400/402/404/422 (rechazo previo a la generación).
    Un proveedor no debe cambiar de modelo ni de proveedor para "reintentar".

Concurrencia: un semáforo limita las llamadas simultáneas; sin cupo dentro del tiempo límite, LLMBusyError
sin llamar a la API.

Uso sin tokens informados: se asume el peor caso (entrada estimada y salida máxima) y nunca cero. Así el costo
registrado no queda subestimado.

Logs: solo identificadores, modelo, estado HTTP, conteos de tokens y códigos. Nunca claves, prompts ni respuestas.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import httpx

from app.integrations.llm import (
    LLMAuthError,
    LLMBusyError,
    LLMDisabledError,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
)

logger = logging.getLogger("lacusine.agent.openrouter")
PROVIDER_NAME = "openrouter"
OFFICIAL_BASE_URL = "https://openrouter.ai/api/v1"
BACKOFF_BASE_SECONDS = 0.5
MAX_RETRIES_LIMIT = 2
# Estimación de tokens de entrada cuando el proveedor no informa uso (misma regla que el orquestador).
CHARS_PER_TOKEN_ESTIMATE = 3

REJECTED_STATUSES = frozenset({400, 402, 404, 422})


class LLMRejectedError(LLMError):
    """El proveedor rechazó la solicitud antes de generar. No facturada."""

    code = "provider_rejected"
    billed_state = "not_billed"


class LLMProviderUnavailableError(LLMError):
    """5xx o estado no contemplado: la generación pudo ocurrir. Facturación incierta, sin reintento."""

    code = "provider_unavailable"


class LLMInvalidResponseError(LLMError):
    """El proveedor respondió, pero sin un objeto utilizable. La generación pudo facturarse."""

    code = "invalid_response"


class OpenRouterAgentClient:
    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 20.0,
        max_retries: int = 1,
        max_concurrency: int = 2,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LLMDisabledError("missing_api_key")
        if not model:
            raise LLMDisabledError("missing_model")
        if max_retries < 0 or max_retries > MAX_RETRIES_LIMIT:
            raise ValueError(f"max_retries debe estar entre 0 y {MAX_RETRIES_LIMIT}")
        if max_concurrency < 1:
            raise ValueError("max_concurrency debe ser al menos 1")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._client = http_client or httpx.Client(
            base_url=OFFICIAL_BASE_URL,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        # La clave nunca aparece en representaciones ni en trazas.
        return f"OpenRouterAgentClient(model={self.model!r})"

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
                logger.warning("llm_retry provider=openrouter call_id=%s code=%s attempt=%d",
                               request.call_id, exc.code, attempt + 1)
                self._sleep(delay)
                attempt += 1

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        messages: list[dict[str, str]] = [{"role": "system", "content": request.system}]
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }

    def _call(self, request: LLMRequest) -> LLMResponse:
        body = self._payload(request)
        try:
            response = self._client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=body,
            )
        except httpx.TimeoutException:
            # Un timeout puede ocurrir después de que la API generó y facturó: nunca se reintenta.
            raise LLMTimeoutError() from None
        except httpx.RequestError:
            # No se sabe si la solicitud llegó: facturación incierta.
            raise LLMError("connection_failed") from None

        status = response.status_code
        if status in (401, 403):
            logger.warning("llm_auth_rejected provider=openrouter call_id=%s status=%d", request.call_id, status)
            raise LLMAuthError() from None
        if status == 429:
            logger.warning("llm_rate_limited provider=openrouter call_id=%s", request.call_id)
            raise LLMRateLimitError() from None
        if status in REJECTED_STATUSES:
            logger.warning("llm_rejected provider=openrouter call_id=%s status=%d", request.call_id, status)
            raise LLMRejectedError() from None
        if status >= 300:
            logger.warning("llm_unavailable provider=openrouter call_id=%s status=%d", request.call_id, status)
            raise LLMProviderUnavailableError() from None

        return self._parse(request, response, body)

    def _parse(self, request: LLMRequest, response: httpx.Response, body: dict[str, Any]) -> LLMResponse:
        try:
            payload = response.json()
        except ValueError:
            raise LLMInvalidResponseError() from None
        if not isinstance(payload, dict):
            raise LLMInvalidResponseError() from None
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMInvalidResponseError() from None
        message = choices[0].get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise LLMInvalidResponseError() from None

        usage = _usage_from(payload.get("usage"))
        if usage is None:
            # Sin uso informado: peor caso, nunca cero.
            usage = _worst_case_usage(body, request.max_output_tokens)
            logger.warning("llm_usage_estimated provider=openrouter call_id=%s", request.call_id)

        result = LLMResponse(
            text=text.strip(),
            model=self.model,
            provider=PROVIDER_NAME,
            usage=usage,
            truncated=choices[0].get("finish_reason") == "length",
        )
        logger.info("llm_call_completed provider=openrouter call_id=%s model=%s in=%d out=%d",
                    request.call_id, self.model, usage.input_tokens, usage.output_tokens)
        return result


def _usage_from(raw: object) -> LLMUsage | None:
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    if not _is_count(prompt) or not _is_count(completion):
        return None
    return LLMUsage(input_tokens=prompt, output_tokens=completion)


def _is_count(value: object) -> bool:
    # bool es subclase de int en Python; un 12.0 de JSON no es un conteo de tokens.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _worst_case_usage(body: dict[str, Any], max_output_tokens: int) -> LLMUsage:
    chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
    return LLMUsage(input_tokens=chars // CHARS_PER_TOKEN_ESTIMATE + 50, output_tokens=max_output_tokens)
