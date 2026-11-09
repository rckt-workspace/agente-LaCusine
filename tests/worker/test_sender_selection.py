"""Selección explícita del remitente y perfil con la clase del objeto realmente construido.

Con WHATSAPP_TRANSPORT=web_bridge nunca debe construirse CloudApiSender, aunque falten credenciales de Meta.
"""

from app.core.config import Settings
from app.integrations.web_bridge import WebBridgeSender
from app.integrations.whatsapp_client import CloudApiSender
from app.worker.bootstrap import select_sender
from app.worker.config import WorkerSettings
from app.worker.profile import describe_worker_profile, format_profile

LINE = "573153863179"
SECRET = "s3cr3t-de-prueba-de-32-caracteres!!"


def _settings(**overrides) -> Settings:
    base = dict(
        meta_app_secret=None, meta_verify_token=None, database_url=None,
        whatsapp_transport="web_bridge", web_bridge_url="http://127.0.0.1:8787", web_bridge_secret=SECRET,
        web_bridge_line_id=LINE, web_bridge_auto_reply=True, whatsapp_audience_mode="test_allowlist",
        whatsapp_allowed_recipients=(), web_bridge_inbound_mode="web_inbound_all",
        web_bridge_inbound_until=None,
    )
    base.update(overrides)
    return Settings(**base)


def test_web_bridge_transport_builds_the_web_sender_without_meta_credentials():
    # Sin ningún dato de Meta: si la selección cayera en Cloud API, esto fallaría.
    sender = select_sender(_settings(meta_access_token=None, meta_phone_number_id=None))
    assert type(sender) is WebBridgeSender
    assert not isinstance(sender, CloudApiSender)


def test_cloud_api_transport_builds_the_cloud_sender():
    sender = select_sender(_settings(whatsapp_transport="cloud_api", meta_access_token="t",
                                     meta_phone_number_id="123456789012", meta_graph_api_version="v20.0"))
    assert type(sender) is CloudApiSender


def test_unknown_transport_is_refused_instead_of_falling_back_to_meta():
    try:
        select_sender(_settings(whatsapp_transport="sms"))
    except ValueError as exc:
        assert str(exc) == "transport_unknown"
    else:
        raise AssertionError("un transporte desconocido no debe construir ningún remitente")


def test_profile_reports_the_transport_and_the_real_sender_class():
    settings = _settings()
    sender = select_sender(settings)
    fields = describe_worker_profile(settings, WorkerSettings(enabled=True, owner="w"), None, sender=sender)
    assert fields["whatsapp_transport"] == "web_bridge"
    assert fields["sender_class"] == "WebBridgeSender"
    line = format_profile(fields)
    assert "whatsapp_transport=web_bridge" in line
    assert "sender_class=WebBridgeSender" in line
    assert SECRET not in line and LINE not in line


def test_profile_without_a_sender_says_so_explicitly():
    fields = describe_worker_profile(_settings(), WorkerSettings(enabled=True, owner="w"), None)
    assert fields["sender_class"] is None
    assert "sender_class=-" in format_profile(fields)
