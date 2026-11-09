"""Reglas puras de la cancelación administrativa de respuestas obsoletas (scripts/cancelar_pendientes_outbox.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cancelar", ROOT / "scripts" / "cancelar_pendientes_outbox.py")
cancelar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cancelar)


def _row(**over):
    base = {"id": "11111111-1111-1111-1111-111111111111", "conversation_id": "d2", "delivery_status": "pending",
            "attempts": 0, "leased": False, "has_wamid": False, "has_sent_at": False, "last_error_code": None,
            "created_at": None}
    base.update(over)
    return base


def test_only_a_pristine_pending_reply_is_eligible():
    assert cancelar.eligibility(_row()) == (True, "elegible")


def test_every_other_state_or_trace_of_delivery_blocks_the_cancellation():
    assert cancelar.eligibility(_row(delivery_status="uncertain")) == (False, "estado_uncertain")
    assert cancelar.eligibility(_row(delivery_status="sent")) == (False, "estado_sent")
    assert cancelar.eligibility(_row(delivery_status="sending")) == (False, "estado_sending")
    assert cancelar.eligibility(_row(attempts=1)) == (False, "con_intentos")
    assert cancelar.eligibility(_row(leased=True)) == (False, "con_lease")
    assert cancelar.eligibility(_row(has_wamid=True)) == (False, "con_identificador_de_meta")
    assert cancelar.eligibility(_row(has_sent_at=True)) == (False, "con_fecha_de_envio")
    assert cancelar.eligibility(_row(last_error_code="send_uncertain")) == (False, "con_error_previo")


def test_the_fingerprint_changes_when_any_row_changes_so_a_stale_preview_cannot_be_applied():
    a = _row(id="11111111-1111-1111-1111-111111111111")
    b = _row(id="22222222-2222-2222-2222-222222222222")
    token = cancelar.fingerprint([a, b])
    assert cancelar.fingerprint([b, a]) == token, "independiente del orden"
    assert cancelar.fingerprint([a, _row(id=b["id"], attempts=1)]) != token
    assert cancelar.fingerprint([a]) != token, "cambiar el conjunto cambia la huella"
