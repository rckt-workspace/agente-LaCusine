"""Respuesta comercial compacta y con datos del catálogo. Proveedor simulado (FakeLLMProvider): sin red, sin cobro.

Estas pruebas validan las reglas del backend con respuestas simuladas. No demuestran el consumo real de tokens
de Anthropic: eso solo lo confirma una prueba real autorizada.
"""

import logging
import uuid

from app.agent.orchestrator import OutcomeStatus
from app.agent.safety import SAFE_REPLY_RETRY, SAFE_REPLY_UNSAFE_OUTPUT
from app.services.product_search import ProductSearchService
from app.integrations.llm import FakeLLMProvider, LLMResponse, LLMUsage
from tests.agent.fakes import InMemoryProductRepository, make_product
from tests.agent.test_orchestrator import Harness, reply

ROWS = [
    make_product("JURA-C3", "Cafetera Automática TEST C3", "Jura", "Cafeteras automáticas", 2990000),
    make_product("JURA-E6", "Cafetera Automática TEST E6", "Jura", "Cafeteras automáticas", 5990000),
    make_product("JURA-E8", "Cafetera Automática TEST E8", "Jura", "Cafeteras automáticas", 10899000),
    make_product("JURA-C9", "Cafetera Automática TEST C9", "Jura", "Cafeteras automáticas", 7590000),
    make_product("JURA-ENA8", "Cafetera Automática TEST ENA8", "Jura", "Cafeteras automáticas", 7999900),
    make_product("JURA-FILTRO", "Cartucho TEST Filtro Jura", "Jura", "Accesorios cafeteras", 102700),
    make_product("JURA-PASTILLA", "Pastillas TEST limpieza Jura", "Jura", "Accesorios cafeteras", 74800),
]
SHORT_REPLY = "Te recomiendo la Jura C3 para espresso diario, es compacta y fácil de usar."


def _harness(provider=None):
    h = Harness(provider)
    repo = InMemoryProductRepository(ROWS)
    h.product_repo = repo
    h.agent._products = repo
    h.agent._search = ProductSearchService(repo)
    return h


def _ids(*refs):
    return [hit.id for hit, _ in ROWS if hit.reference in refs]


def _user_content(h):
    return h.provider.calls[-1].messages[0].content


def _text_of_length(chars):
    base = "Te recomiendo la Jura C3 para espresso diario. "
    return (base * (chars // len(base) + 1))[:chars]


def test_cafetera_jura_sends_exactly_five_candidates_to_the_model():
    h = _harness()
    h.provider.script = [reply(SHORT_REPLY, ids=_ids("JURA-C3"))]
    h.ask("quiero una cafetera Jura")
    content = _user_content(h)
    assert content.count("- id:") == 5
    assert "Cafetera Automática TEST C3" in content


def test_model_selects_two_valid_models_and_both_are_kept():
    h = _harness()
    ids = _ids("JURA-C3", "JURA-E6")
    h.provider.script = [reply("Para espresso, la C3 o la E6 son buenas opciones.", ids=ids)]
    outcome = h.ask("cafetera Jura para espresso")
    assert outcome.status is OutcomeStatus.REPLIED
    assert set(outcome.product_ids) == set(ids)


def test_invented_identifier_is_dropped_and_real_ones_are_kept():
    h = _harness()
    real = _ids("JURA-C3")
    invented = uuid.uuid4()
    h.provider.script = [reply(SHORT_REPLY, ids=[*real, invented])]
    outcome = h.ask("cafetera Jura")
    assert outcome.status is OutcomeStatus.REPLIED
    assert outcome.product_ids == tuple(real)
    assert invented not in outcome.product_ids


def test_compact_reply_of_about_500_characters_is_accepted():
    h = _harness()
    h.provider.script = [reply(_text_of_length(500), ids=_ids("JURA-C3"))]
    outcome = h.ask("cafetera Jura")
    assert outcome.status is OutcomeStatus.REPLIED
    assert 450 <= len(outcome.reply_text) <= 550


def test_reply_over_the_schema_limit_is_rejected_and_never_sent_as_is():
    h = _harness()
    h.provider.script = [reply(_text_of_length(1250), ids=_ids("JURA-C3"))]
    outcome = h.ask("cafetera Jura")
    assert outcome.status is OutcomeStatus.INVALID_OUTPUT
    assert outcome.reply_text is None
    assert h.handoffs.opened == []  # recuperable: no transfiere


def test_truncated_output_is_recoverable_without_transfer():
    class Truncating(FakeLLMProvider):
        def complete(self, request):
            response = super().complete(request)
            return LLMResponse(text=response.text, model=response.model, provider=response.provider,
                               usage=LLMUsage(input_tokens=4000, output_tokens=1100), truncated=True)

    h = _harness(Truncating(script=[reply(SHORT_REPLY)]))
    outcome = h.ask("cafetera Jura")
    assert outcome.status is OutcomeStatus.INVALID_OUTPUT
    assert outcome.error_code == "truncated"
    assert h.handoffs.opened == []


def test_follow_up_uses_real_history_and_keeps_the_conversation_automated():
    h = _harness()
    h.turns.add(h.conversation_id, "inbound", "Quiero una cafetera Jura")
    h.turns.add(h.conversation_id, "outbound", "Te recomiendo la Jura C3 y la E6.")
    h.provider.script = [reply("La C3 es ideal para espresso diario.", ids=_ids("JURA-C3"))]
    outcome = h.ask("¿Cuál me sirve para espresso?")
    assert outcome.status is OutcomeStatus.REPLIED
    content = _user_content(h)
    assert "Quiero una cafetera Jura" in content
    assert "Te recomiendo la Jura C3 y la E6." in content
    assert h.conversations.states[h.conversation_id] == "automated"


def test_previous_error_texts_are_not_resent_as_history():
    h = _harness()
    h.turns.add(h.conversation_id, "inbound", "¿Qué cafeteras tienes?")
    h.turns.add(h.conversation_id, "outbound", SAFE_REPLY_RETRY)
    h.provider.script = [reply("Tengo varias cafeteras Jura.", ids=_ids("JURA-C3"))]
    h.ask("¿Y cuál recomiendas?")
    assert SAFE_REPLY_RETRY not in _user_content(h)


def test_unauthorized_link_in_the_reply_is_blocked_and_replaced_by_the_safe_text():
    h = _harness()
    h.provider.script = [reply("Compra aquí: https://tienda-falsa.example.com/jura-c3", ids=_ids("JURA-C3"))]
    outcome = h.ask("cafetera Jura")
    assert outcome.status is OutcomeStatus.UNSAFE_OUTPUT
    assert outcome.reply_text == SAFE_REPLY_UNSAFE_OUTPUT
    assert "tienda-falsa" not in (outcome.reply_text or "")


def test_output_stats_are_logged_without_reply_content(caplog):
    h = _harness()
    private_like = "Te recomiendo la Jura C3 para tu cliente llamado Ana Pérez"
    h.provider.script = [reply(private_like, ids=_ids("JURA-C3"))]
    with caplog.at_level(logging.INFO, logger="lacusine.agent"):
        h.ask("cafetera Jura")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "llm_output_stats" in text and "out_tokens=" in text and "chars=" in text
    assert "Ana Pérez" not in text and "Te recomiendo" not in text


def test_system_prompt_states_the_compact_commercial_rules():
    system = _harness().agent._system_prompt
    assert "entre 1 y 3 productos" in system
    assert "350 a 600 caracteres" in system
    assert "nunca accesorios" in system
    assert "historial solo para mantener el hilo" in system
