"""Clasificación de errores del procesamiento. Los códigos son estables y no contienen datos."""

from __future__ import annotations

from enum import Enum

from app.repositories.errors import StorageError


class ErrorClass(str, Enum):
    TRANSIENT = "transient"      # reintento con backoff, hasta max_attempts
    PERMANENT = "permanent"      # estado terminal: dead_letter
    UNCERTAIN = "uncertain"      # resultado externo incierto: revisión manual, nunca reenvío automático


class PermanentJobError(Exception):
    """El evento no puede procesarse nunca (payload imposible, datos inválidos). Código estable, sin contenido."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ManualReviewRequired(Exception):
    """El procesamiento no puede continuar sin una persona (resultado incierto, estado de reserva ambiguo)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def classify(exc: BaseException) -> tuple[ErrorClass, str]:
    if isinstance(exc, PermanentJobError):
        return ErrorClass.PERMANENT, exc.code
    if isinstance(exc, ManualReviewRequired):
        return ErrorClass.UNCERTAIN, exc.code
    if isinstance(exc, StorageError):
        return ErrorClass.TRANSIENT, exc.args[0] if exc.args else "storage_error"
    # Cualquier otro fallo se trata como transitorio y se registra solo su tipo, nunca su mensaje.
    return ErrorClass.TRANSIENT, "internal_error"
