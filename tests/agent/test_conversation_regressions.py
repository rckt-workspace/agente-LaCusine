"""Regresiones de conversación: la IA no se detiene por saludos, preguntas de catálogo ni intención de compra.

Proveedor real simulado con FakeLLMProvider (sin red ni cobro). Todo en memoria.
"""

from app.agent.orchestrator import OutcomeStatus
from app.agent.safety import SAFE_REPLY_HUMAN
from app.integrations.llm import FakeLLMProvider
from tests.agent.test_orchestrator import Harness, reply


def _conversation_state(h: Harness) -> str:
    return h.conversations.states[h.conversation_id]


def test_four_consecutive_messages_keep_the_conversation_automated_and_each_gets_its_own_reply():
    h = Harness(FakeLLMProvider(script=[
        reply("¡Hola! Soy el asistente de La Cuisine. ¿En qué te ayudo?", intent="greeting"),
        reply("¡Muy bien, gracias! ¿Qué electrodoméstico buscas hoy?", intent="other"),
        reply("Tenemos cafeteras; ¿para cuántas personas la necesitas?", intent="product_inquiry",
              stage="DISCOVERY"),
        reply("Perfecto, para dos personas puedes ver las opciones compactas. El pago se completa en la web "
              "o con un asesor cuando decidas.", intent="purchase_intent", stage="HIGH_INTENT"),
    ]))
    texts = ["Hola", "¿Cómo estás?", "¿Qué cafeteras tienes?", "Quiero comprar una cafetera para dos personas."]
    outcomes = [h.ask(text) for text in texts]

    assert [o.status for o in outcomes] == [OutcomeStatus.REPLIED] * 4
    assert all(o.requires_human is False for o in outcomes)
    assert h.handoffs.opened == []
    assert _conversation_state(h) == "automated"
    assert len({o.reply_text for o in outcomes}) == 4  # respuestas independientes, no una repetida


def test_model_marking_requires_human_on_a_greeting_does_not_transfer_or_pause():
    h = Harness(FakeLLMProvider(script=[
        reply("¡Hola! ¿Qué buscas hoy?", intent="other", requires_human=True, reason="unsupported_request"),
    ]))
    outcome = h.ask("Hola")
    assert outcome.status is OutcomeStatus.REPLIED
    assert outcome.requires_human is False
    assert outcome.reply_text == "¡Hola! ¿Qué buscas hoy?"  # se envía la respuesta real, no el texto de asesor
    assert h.handoffs.opened == []
    assert _conversation_state(h) == "automated"


def test_explicit_request_for_an_advisor_still_transfers():
    h = Harness()
    outcome = h.ask("Quiero hablar con un asesor, por favor")
    assert outcome.requires_human is True
    assert outcome.reply_text == SAFE_REPLY_HUMAN
    assert [r for _, r, _ in h.handoffs.opened] == ["customer_request"]


def test_payment_question_still_transfers_to_an_advisor():
    h = Harness()
    outcome = h.ask("¿Cómo pago la cafetera?")
    assert outcome.requires_human is True
    assert [r for _, r, _ in h.handoffs.opened] == ["payment_request"]


def test_purchase_intent_alone_keeps_advising_and_is_not_a_transfer():
    h = Harness(FakeLLMProvider(script=[
        reply("Con gusto. ¿Cuál es tu presupuesto aproximado?", intent="purchase_intent", stage="HIGH_INTENT"),
    ]))
    outcome = h.ask("Quiero comprar una cafetera")
    assert outcome.requires_human is False
    assert h.handoffs.opened == []
    assert _conversation_state(h) == "automated"
