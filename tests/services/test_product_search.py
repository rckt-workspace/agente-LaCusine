"""Búsqueda comercial sobre fixtures sintéticos (sin base de datos)."""

from app.services.product_search import MAX_RESULTS, ProductSearchService
from tests.agent.fakes import InMemoryProductRepository, product_fixtures


def _service():
    repo = InMemoryProductRepository(product_fixtures())
    return ProductSearchService(repo), repo


def test_name_and_keyword_search_returns_relevant_products_first():
    service, _ = _service()
    result = service.search("cafetera Jura")
    assert result.hits[0].reference == "TEST-JURA-C3"
    assert result.confident is True


def test_reference_exact_match_is_confident_and_ranked_first():
    service, _ = _service()
    result = service.search("quiero la TEST-JURA-E6")
    assert result.hits[0].reference == "TEST-JURA-E6"
    assert result.hits[0].reference_match is True
    assert result.confident is True


def test_minor_typo_is_corrected_against_catalog_vocabulary():
    service, _ = _service()
    result = service.search("nevara side by side")
    assert result.corrections == {"nevara": "nevera"}
    assert result.hits and result.hits[0].reference == "TEST-WHP-SBS"


def test_unknown_product_is_not_confident_and_returns_nothing():
    service, _ = _service()
    result = service.search("microondas zeta")
    assert result.hits == []
    assert result.confident is False


def test_half_of_useful_terms_is_enough_for_confidence():
    service, _ = _service()
    result = service.search("cafetera jura espresso luna")
    assert result.confident is True


def test_budget_is_passed_to_repository_as_upper_bound():
    service, repo = _service()
    service.search("nevera side by side", max_price_cop=4_000_000)
    assert repo.search_calls[-1]["max_price"] == 4_000_000


def test_results_are_capped_to_five_even_when_more_are_requested():
    service, repo = _service()
    service.search("cafetera nevera Jura Smeg Whirlpool", limit=50)
    assert repo.search_calls[-1]["limit"] == MAX_RESULTS == 5


def test_empty_query_does_not_touch_repository():
    service, repo = _service()
    result = service.search("hola buenas")
    assert result.hits == []
    assert repo.search_calls == []


def test_vocabulary_is_loaded_once_and_reused():
    service, repo = _service()
    calls = {"n": 0}
    original = repo.search_texts

    def counting():
        calls["n"] += 1
        return original()

    repo.search_texts = counting
    service.search("nevera")
    service.search("cafetera")
    assert calls["n"] == 1


def test_full_term_match_wins_even_when_the_product_is_out_of_stock():
    service, _ = _service()
    result = service.search("nevera retro Smeg")
    assert result.hits[0].reference == "TEST-SMEG-50"  # coincidencia completa, aunque agotado
