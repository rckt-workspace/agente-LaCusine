"""Memoria conversacional: el modelo ve lo que ya recomendó y las preguntas de seguimiento usan el contexto.

Simulado: FakeLLMProvider (sin red) y catálogo sintético. El lector de turnos reproduce la semántica de producción:
mensajes del cliente ("inbound") y respuestas enviadas o inciertas del agente ("outbound").
"""

from app.integrations.llm import FakeLLMProvider
from app.agent.orchestrator import OutcomeStatus
from tests.agent.test_orchestrator import Harness, reply


def _remember(h: Harness, text: str, direction: str) -> None:
    h.turns.add(h.conversation_id, direction, text)


def test_followup_uses_the_products_already_recommended_instead_of_saying_there_is_no_information():
    first_answer = "Tenemos la Jura TEST C3 y la Jura TEST E6, ambas de espresso."
    h = Harness(FakeLLMProvider(script=[reply(first_answer), reply("Para casa, la C3 es una buena opción.")]))

    q1 = "¿Cuáles cafeteras Jura tienen disponibles?"
    o1 = h.ask(q1)
    assert o1.status is OutcomeStatus.REPLIED
    _remember(h, q1, "inbound")
    _remember(h, o1.reply_text, "outbound")

    q2 = "¿Cuál me recomiendas para mi casa?"
    _remember(h, q2, "inbound")
    h.ask(q2)

    second_request = h.provider.calls[1].messages[0].content
    assert 'busqueda_confiable="true"' in second_request, "la búsqueda con contexto debe ser confiable"
    assert "TEST-JURA-C3" in second_request and "TEST-JURA-E6" in second_request, "candidatos Jura del turno anterior"
    assert "Tenemos la Jura TEST C3" in second_request, "la respuesta anterior del agente llega en el historial"
    assert "sin candidatos" not in second_request


def test_the_previous_answer_of_the_agent_is_part_of_the_history_sent_to_the_model():
    h = Harness(FakeLLMProvider(script=[reply("Hola, soy el asistente de La Cuisine. Tenemos la C3."), reply("Claro.")]))
    o1 = h.ask("¿Qué cafeteras Jura tienen?")
    _remember(h, "¿Qué cafeteras Jura tienen?", "inbound")
    _remember(h, o1.reply_text, "outbound")
    h.ask("¿Y cuál prefieres?")
    content = h.provider.calls[1].messages[0].content
    assert "[agente] Hola, soy el asistente de La Cuisine" in content


def test_a_follow_up_without_any_product_context_keeps_the_original_search_result():
    h = Harness(FakeLLMProvider(script=[reply("¿Qué necesitas?")]))
    h.ask("¿Cuál me recomiendas?")
    content = h.provider.calls[0].messages[0].content
    assert 'busqueda_confiable="false"' in content, "sin contexto de producto no se inventa una búsqueda"
