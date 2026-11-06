"""Relevancia comercial: la máquina que se busca gana a los accesorios; el precio no decide sin pedir «económica».

Datos SINTÉTICOS con la misma forma que el catálogo real (categoría, marca, referencia, precio). Sin base de datos.
El repositorio simulado replica la SQL: coincidencia por subcadena, orden por precio y recorte al límite.
"""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid5

from app.repositories.products import CANDIDATE_LIMIT, MAX_RESULTS, ProductHit
from app.services.product_search import ProductSearchService, query_intent, rank_candidates
from app.services.text import normalize, tokens, words

NS = UUID("00000000-0000-4000-8000-00000000abcd")


def _hit(ref, name, brand, category, price, available=True):
    return {"reference": ref, "name": name, "brand": brand, "category": category, "price": price,
            "available": available}


CATALOG = [
    # Máquinas (categoría «Cafeteras automáticas»)
    _hit("15711", "Maquina de Cafe Jura C3 Solo Espresso Negro", "Jura", "Cafeteras automáticas", 2_990_000),
    _hit("15465", "Maquina De Cafe Jura E6 Color Platino", "Jura", "Cafeteras automáticas", 5_990_000),
    _hit("15495", "Maquina De Cafe Jura ENA8 Blanco", "Jura", "Cafeteras automáticas", 7_999_900),
    _hit("15800", "Cafetera Smeg Retro Espresso", "Smeg", "Cafeteras automáticas", 1_500_000),
    # Accesorios (categoría «Accesorios cafeteras») — algunos más baratos que cualquier máquina
    _hit("24224", "Pastillas de Limpieza JURA", "Jura", "Accesorios cafeteras", 74_800),
    _hit("61848", "Pastillas de descalcificacion JURA", "Jura", "Accesorios cafeteras", 89_800),
    _hit("24228OS", "Cartucho Jura De Filtro CLARIS Blue", "Jura", "Accesorios cafeteras", 102_700),
    _hit("24195", "Limpiador del sistema de leche Jura", "Jura", "Accesorios cafeteras", 114_000),
    _hit("24115", "Juego de accesorios JURA para sistemas", "Jura", "Accesorios cafeteras", 119_000),
]


class SqlLikeRepo:
    """Replica la SQL de búsqueda: strpos sobre el texto del producto, orden y límite."""

    def __init__(self, rows=CATALOG):
        self._rows = []
        for row in rows:
            text = normalize(" ".join([row["name"], row["brand"], row["category"], row["reference"]]))
            self._rows.append((row, text))
        self.calls = []

    def search(self, tokens_, references, max_price_cop, limit):
        self.calls.append(limit)
        refs = {r.upper() for r in references}
        out = []
        for row, text in self._rows:
            matched = sum(1 for t in tokens_ if t in text)
            ref_match = row["reference"].upper() in refs
            if matched == 0 and not ref_match:
                continue
            out.append(ProductHit(
                id=uuid5(NS, row["reference"]), reference=row["reference"], name=row["name"], brand=row["brand"],
                category=row["category"], price_cop=Decimal(row["price"]), previous_price_cop=None,
                is_available=row["available"], delivery_note=None, product_url=None, description=None,
                matched_tokens=matched, reference_match=ref_match,
            ))
        out.sort(key=lambda h: (not h.reference_match, -h.matched_tokens, not bool(h.is_available), h.price_cop))
        return out[: min(limit, CANDIDATE_LIMIT)]

    def get_by_ids(self, ids):
        return {}

    def search_texts(self):
        return [text for _, text in self._rows]


def _search(query):
    return ProductSearchService(SqlLikeRepo()).search(query)


def _refs(result, n=MAX_RESULTS):
    return [h.reference for h in result.hits[:n]]


def _is_machine(hit):
    return hit.category.lower().startswith("cafeteras")


# --- Escenarios comerciales ---

def test_quiero_una_cafetera_jura_prioriza_maquinas_jura_no_accesorios():
    result = _search("quiero una cafetera Jura")
    top = result.hits[:3]
    assert len(top) == 3 and all(_is_machine(h) and h.brand == "Jura" for h in top)
    assert {h.reference for h in top} == {"15711", "15465", "15495"}
    assert result.confident is True


def test_necesito_filtros_jura_prioriza_el_filtro_no_las_maquinas():
    result = _search("necesito filtros Jura")
    assert result.hits[0].reference == "24228OS"
    assert not any(_is_machine(h) for h in result.hits[:2])


def test_referencia_jura_c3_recupera_la_maquina_exacta_primero():
    result = _search("Jura C3")
    assert result.hits[0].reference == "15711"


def test_cafetera_para_espresso_encuentra_maquinas_compatibles_primero():
    result = _search("cafetera para espresso")
    assert result.hits[0].reference in {"15711", "15800"}
    assert _is_machine(result.hits[0])
    assert all(_is_machine(h) for h in result.hits[:2])


def test_cafetera_economica_prioriza_maquinas_y_la_mas_barata_entre_ellas():
    result = _search("cafetera económica")
    assert all(_is_machine(h) for h in result.hits[:3])
    assert result.hits[0].reference == "15800"  # la máquina más barata del catálogo de prueba


def test_menor_precio_no_convierte_un_accesorio_en_resultado_principal_sin_pedirlo():
    result = _search("cafetera Jura")
    assert _is_machine(result.hits[0])


# --- Variaciones de escritura ---

def test_mayusculas_tildes_plurales_y_palabras_de_relleno_dan_el_mismo_resultado():
    base = _refs(_search("cafetera Jura"))
    assert _refs(_search("CAFETERA JURÁ")) == base
    assert _refs(_search("cafeteras jura")) == base
    assert _refs(_search("hola, ¿me das una cafetera jura, por favor?")) == base


def test_palabras_conversacionales_no_entran_en_el_criterio_de_coincidencia():
    tokens_found = tokens("quiero comprar una cafetera Jura económica por favor")
    assert "cafetera" in tokens_found and "jura" in tokens_found
    assert "economica" not in tokens_found and "comprar" not in tokens_found


def test_intencion_detecta_accesorio_y_precio_desde_la_consulta_completa():
    raw = words("necesito un filtro Jura económico")
    intent = query_intent(raw, tokens("necesito un filtro Jura económico"))
    assert intent.wants_accessory is True and intent.economy is True
    raw_machine = words("cafetera Jura")
    assert query_intent(raw_machine, tokens("cafetera Jura")).wants_accessory is False


# --- Ausencia real de candidatos y datos ---

def test_ausencia_real_de_candidatos_responde_sin_inventar_productos():
    result = _search("licuadora Oster")
    assert result.hits == []
    assert result.confident is False


def test_hits_are_deduplicated_and_identifiers_are_unique():
    duplicated = CATALOG[:2] + CATALOG[:2]
    repo = SqlLikeRepo(duplicated)
    ranked = rank_candidates(repo.search(["cafetera", "jura"], [], None, CANDIDATE_LIMIT),
                             query_intent(["cafetera", "jura"], ["cafetera", "jura"]))
    ids = [h.id for h in ranked]
    assert len(ids) == len(set(ids)) == 2


def test_model_never_receives_more_than_five_candidates_even_with_forty_fetched():
    many = [_hit(f"9{i:03d}", f"Cafetera Jura modelo {i}", "Jura", "Cafeteras automáticas", 1_000_000 + i)
            for i in range(30)]
    result = ProductSearchService(SqlLikeRepo(CATALOG + many)).search("cafetera Jura")
    assert len(result.hits) == MAX_RESULTS


# --- Regresión: por qué el límite de 5 antes de ordenar fallaba ---

def test_machines_survive_the_candidate_cut_even_when_eight_cheap_accessories_match_first():
    cheap_accessories = [_hit(f"AC{i}", f"Filtro Jura repuesto {i}", "Jura", "Accesorios cafeteras", 36_990 + i)
                         for i in range(8)]
    rows = cheap_accessories + CATALOG
    result = ProductSearchService(SqlLikeRepo(rows)).search("cafetera Jura")
    assert any(_is_machine(h) for h in result.hits)
    assert _is_machine(result.hits[0])


def test_the_old_five_row_limit_would_have_lost_every_machine_in_that_case():
    """Documenta el fallo original: con LIMIT 5 en SQL, el orden por precio dejaba fuera las máquinas."""
    cheap_accessories = [_hit(f"AC{i}", f"Filtro Jura repuesto {i}", "Jura", "Accesorios cafeteras", 36_990 + i)
                         for i in range(8)]
    repo = SqlLikeRepo(cheap_accessories + CATALOG)
    five_row_pool = repo.search(["cafetera", "jura"], [], None, MAX_RESULTS)
    assert not any(_is_machine(h) for h in five_row_pool)


def test_filtros_en_plural_encuentra_el_filtro_real_primero():
    result = _search("necesito filtros Jura")
    assert "filtro" in result.hits[0].name.lower()
    assert result.hits[0].reference == "24228OS"


def test_singular_y_plural_dan_el_mismo_resultado_principal():
    assert _refs(_search("necesito filtro Jura"))[:1] == _refs(_search("necesito filtros Jura"))[:1]
