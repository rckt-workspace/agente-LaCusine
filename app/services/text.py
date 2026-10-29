"""Normalización de texto en español para búsqueda: minúsculas, sin tildes, tokens útiles."""

from __future__ import annotations

import re
import unicodedata

_TOKEN = re.compile(r"[a-z0-9]+")

# Palabras de conversación que no aportan a la búsqueda de producto.
STOPWORDS = frozenset(
    {
        "a", "al", "con", "de", "del", "el", "en", "es", "la", "las", "lo", "los", "o", "para", "por",
        "que", "quiero", "quisiera", "busco", "necesito", "me", "mi", "un", "una", "unos", "unas", "y",
        "precio", "cuanto", "cuánto", "vale", "tienen", "tiene", "hay", "info", "informacion", "favor",
        "hola", "buenas", "gracias", "puede", "podria", "podrian", "dame", "algo", "para", "sobre",
    }
)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    return " ".join(_TOKEN.findall(strip_accents(text).lower()))


def tokens(text: str) -> list[str]:
    """Tokens útiles: de al menos 2 caracteres y sin palabras vacías."""
    return [t for t in _TOKEN.findall(strip_accents(text).lower()) if len(t) >= 2 and t not in STOPWORDS]
