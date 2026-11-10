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
        # Verbos y relleno de compra: no describen el producto.
        "comprar", "compro", "recomienda", "recomiendame", "recomiendeme", "opciones", "opcion", "modelo",
        "modelos", "producto", "productos", "tienes", "tendras", "ocupo", "quisiera", "pueden",
        # Preferencias de precio: se usan para ordenar (ver product_search.ECONOMY_TERMS), no para coincidir.
        "economica", "economico", "economicas", "economicos", "barata", "barato", "baratas", "baratos",
        "asequible",
        # Preguntas y verbos de disponibilidad o venta: no describen el producto (detectados en consultas reales).
        "vende", "venden", "vendes", "vender", "cual", "cuales", "sirve", "sirven", "disponible", "disponibles",
        "existe", "existen", "cuantas", "cuantos", "ayuda", "ayudame",
        # Contexto de uso del cliente: orienta la conversación, no coincide con el catálogo.
        "familia", "familias", "personas", "persona", "casa", "hogar", "oficina", "cuatro", "tres", "dos", "cinco",
        "bonita", "bonito", "facil", "usar", "buena", "bueno", "recomendar", "recomendaciones",
        "recomiendas", "recomiendo", "recomienden",
    }
)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    return " ".join(_TOKEN.findall(strip_accents(text).lower()))


def words(text: str) -> list[str]:
    """Todas las palabras normalizadas, incluidas las vacías (para detectar intención)."""
    return _TOKEN.findall(strip_accents(text).lower())


def tokens(text: str) -> list[str]:
    """Tokens útiles: de al menos 2 caracteres y sin palabras vacías."""
    return [t for t in words(text) if len(t) >= 2 and t not in STOPWORDS]
