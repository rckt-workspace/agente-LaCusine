"""Demostración visual en /demo. Solo para entorno aislado con datos sintéticos.

Principios:
- Nada se muestra ni se ejecuta sin sesión válida. La sesión se obtiene con DEMO_TOKEN (solo variable de entorno).
- Cada acción usa los servicios reales: se persiste un evento del webhook, lo procesa el worker, y las
  transferencias pasan por el repositorio de producción. No hay respuestas inventadas en la interfaz.
- El proveedor de mensajes es el simulado: esta ruta nunca llama a Meta ni a Anthropic.
- Los números son sintéticos y el contenido se escapa en el navegador (textContent).
- Limitación de intentos de acceso y de solicitudes por IP. Sesión firmada con clave aleatoria por proceso.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.repositories.crm import PostgresHandoffRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository, WebhookEventRecord

SESSION_SECONDS = 2 * 60 * 60
LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
API_REQUESTS_PER_MINUTE = 60
MAX_TEXT_CHARS = 500
SYNTHETIC_PREFIX = "5730"
DEMO_LINE_NOTE = "DEMO · DATOS SINTÉTICOS · PROVEEDORES SIMULADOS · NO SE ENVÍAN MENSAJES REALES"

CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


class _Limiter:
    """Ventana deslizante en memoria por clave (IP)."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        q = self._events[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def count(self, key: str, window: float) -> int:
        now = time.monotonic()
        q = self._events[key]
        while q and now - q[0] > window:
            q.popleft()
        return len(q)


class _Session:
    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        mac = hmac.new(self._key, str(expires).encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{mac}"

    def valid(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        expires_raw, mac = cookie.split(".", 1)
        if not expires_raw.isdigit() or int(expires_raw) < time.time():
            return False
        expected = hmac.new(self._key, expires_raw.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, expected)


class LoginIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    referral: bool = False
    # Contacto sintético del visitante. Reutilizarlo mantiene la misma conversación del mismo cliente.
    contact: str | None = Field(default=None, pattern=r"^5730[0-9]{8}$")


class HandoffIn(BaseModel):
    conversation_id: uuid.UUID


def _mask(contact: str | None) -> str:
    if not contact:
        return "—"
    return "•" * max(0, len(contact) - 4) + contact[-4:]


def _synthetic_contact() -> str:
    return SYNTHETIC_PREFIX + "".join(secrets.choice("0123456789") for _ in range(8))


def build_demo_router(*, token: str, pool, line: str, retention_days: int) -> APIRouter:
    """Construye la ruta /demo. Solo se llama si DEMO_ENABLED=true, con token, base y línea configurados."""
    router = APIRouter(prefix="/demo", tags=["demo"], include_in_schema=False)
    session = _Session()
    login_limiter = _Limiter()
    api_limiter = _Limiter()
    events = PostgresWebhookEventRepository(pool, retention_days=retention_days)
    handoffs = PostgresHandoffRepository(pool)
    expected_digest = hashlib.sha256(token.encode()).digest()

    def _client(request: Request) -> str:
        return request.client.host if request.client else "desconocido"

    def _secure_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _require_same_origin(request: Request) -> None:
        """Defensa frente a CSRF: JSON obligatorio y origen igual al del servidor cuando el navegador lo indica."""
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    def _require_session(request: Request) -> None:
        if not session.valid(request.cookies.get("demo_session")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if not api_limiter.hit(_client(request), API_REQUESTS_PER_MINUTE, 60):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    @router.get("", response_class=HTMLResponse)
    def page(request: Request) -> Response:
        if not session.valid(request.cookies.get("demo_session")):
            return _secure_headers(HTMLResponse(LOGIN_HTML))
        return _secure_headers(HTMLResponse(APP_HTML.replace("__NOTE__", DEMO_LINE_NOTE)))

    @router.get("/app.js")
    def script(request: Request) -> Response:
        if not session.valid(request.cookies.get("demo_session")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return _secure_headers(PlainTextResponse(APP_JS, media_type="application/javascript"))

    @router.post("/login")
    def login(body: LoginIn, request: Request) -> Response:
        _require_same_origin(request)
        key = _client(request)
        if login_limiter.count(key, LOGIN_WINDOW_SECONDS) >= LOGIN_FAILURES:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)
        if not hmac.compare_digest(hashlib.sha256(body.token.encode()).digest(), expected_digest):
            login_limiter.hit(key, 10_000, LOGIN_WINDOW_SECONDS)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        response = JSONResponse({"ok": True})
        response.set_cookie("demo_session", session.issue(), max_age=SESSION_SECONDS, httponly=True,
                            secure=True, samesite="strict", path="/demo")
        return _secure_headers(response)

    @router.post("/api/message")
    def message(body: MessageIn, request: Request) -> Response:
        _require_same_origin(request)
        _require_session(request)
        if line is None or line == "":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        contact = body.contact or _synthetic_contact()
        wamid = f"wamid.DEMO-{uuid.uuid4().hex}"
        message_body = {"from": contact, "id": wamid, "timestamp": str(int(time.time())), "type": "text",
                        "text": {"body": body.text}}
        if body.referral:
            message_body["referral"] = {"source_type": "ad", "source_id": "120200000DEMO",
                                        "source_url": "https://example.invalid/demo-sintetico",
                                        "ctwa_clid": "clid-demo-sintetico"}
        canonical = json.dumps(message_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        record = WebhookEventRecord(
            external_event_id=wamid, contact_wa_id=contact, payload=message_body,
            payload_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(), business_phone_id=line,
        )
        outcome = events.register(record)
        # El número es sintético: se devuelve completo para que la interfaz lo reutilice en el siguiente mensaje.
        return _secure_headers(JSONResponse({"accepted": outcome.name.lower(), "contact": contact}))

    @router.post("/api/handoff")
    def handoff(body: HandoffIn, request: Request) -> Response:
        _require_same_origin(request)
        _require_session(request)
        handoff_id = handoffs.open(body.conversation_id, "customer_request", "normal")
        return _secure_headers(JSONResponse({"handoff": str(handoff_id), "automation": "paused_for_human"}))

    @router.get("/api/state")
    def state(request: Request) -> Response:
        _require_session(request)
        with pool.connection() as conn:
            def rows(sql):
                return conn.execute(sql).fetchall()

            data = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "events": dict(rows("select processing_status, count(*) from public.webhook_events group by 1")),
                "conversations": dict(rows(
                    "select automation_state, count(*) from public.conversations group by 1")),
                "notifications": dict(rows(
                    "select status, count(*) from public.advisor_notifications group by 1")),
                "leads": [
                    {"contact": _mask(r[0]), "status": r[1], "interest": r[2],
                     "attribution": r[3] or None, "since": r[4].isoformat()}
                    for r in rows(
                        "select l.wa_contact_id, l.status, l.product_interest, l.referral_source_id, l.created_at "
                        "from public.leads l order by l.created_at desc limit 10")
                ],
                "replies": [
                    {"kind": r[0], "delivery": r[1], "text": (r[2] or "")[:300], "at": r[3].isoformat()}
                    for r in rows(
                        "select kind, delivery_status, content_text, created_at from public.outbound_messages "
                        "order by created_at desc limit 10")
                ],
                "open_conversations": [
                    {"id": str(r[0]), "state": r[1]}
                    for r in rows(
                        "select id, automation_state from public.conversations "
                        "where status <> 'closed' and automation_state = 'automated' "
                        "order by last_activity_at desc limit 5")
                ],
                "handoffs": [
                    {"reason": r[0], "priority": r[1], "status": r[2], "at": r[3].isoformat()}
                    for r in rows(
                        "select reason, priority, status, opened_at from public.human_handoffs "
                        "order by opened_at desc limit 5")
                ],
            }
        return _secure_headers(JSONResponse(data))

    return router


LOGIN_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Demo LCA</title>
<style>body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#0f1a2a;color:#e8eef7}
main{max-width:420px;margin:12vh auto}input,button{font:inherit;padding:10px;width:100%;box-sizing:border-box;margin-top:8px}
button{background:#2f7dd1;color:#fff;border:0;border-radius:6px;cursor:pointer}.note{color:#9fb3c8;font-size:13px}</style>
</head><body><main><h1>Demo del agente</h1><p class="note">Acceso restringido. Datos sintéticos.</p>
<form id="login"><label>Token de demostración<input id="token" type="password" autocomplete="off" required></label>
<button type="submit">Entrar</button></form><p id="msg" class="note"></p></main>
<script>
document.getElementById('login').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/demo/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token: document.getElementById('token').value})});
  if (r.ok) { location.reload(); } else { document.getElementById('msg').textContent = 'Acceso no válido o demasiados intentos.'; }
});
</script></body></html>"""

APP_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Demo LCA</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#0f1a2a;color:#e8eef7}
h1{font-size:20px;margin:0 0 4px}.banner{background:#7a4b00;color:#fff;padding:8px;border-radius:6px;font-size:13px}
.grid{display:grid;gap:12px;grid-template-columns:1fr}@media(min-width:900px){.grid{grid-template-columns:1fr 1fr}}
section{background:#16263d;border-radius:10px;padding:14px}h2{font-size:15px;margin:0 0 8px;color:#9fd0ff}
textarea{width:100%;min-height:70px;box-sizing:border-box;font:inherit;padding:8px;border-radius:6px;border:1px solid #2d4766;background:#0f1a2a;color:#e8eef7}
button{font:inherit;padding:8px 12px;border:0;border-radius:6px;background:#2f7dd1;color:#fff;cursor:pointer;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #2d4766;padding:4px;text-align:left;vertical-align:top}
.muted{color:#9fb3c8;font-size:12px}
</style></head><body>
<div class="banner">__NOTE__</div>
<h1>Agente comercial · demostración</h1>
<div class="grid">
<section><h2>1. Cliente escribe por WhatsApp (simulado)</h2>
<textarea id="text">Hola, estoy buscando una cafetera para mi casa. ¿Qué opciones tienen?</textarea>
<label class="muted"><input type="checkbox" id="referral"> Llega desde un anuncio (atribución sintética)</label><br>
<button id="send">Enviar mensaje sintético</button>
<p class="muted" id="sent"></p></section>
<section><h2>2. Transferencia a asesor</h2>
<p class="muted">Abre una solicitud sobre la última conversación. La automatización se pausa y los mensajes nuevos no reciben respuesta automática.</p>
<button id="handoff">Transferir a asesor</button><p class="muted" id="handoffmsg"></p></section>
<section style="grid-column:1/-1"><h2>3. Estado del procesamiento</h2><div id="state" class="muted">Cargando…</div></section>
</div>
<script src="/demo/app.js"></script></body></html>"""

APP_JS = """
let syntheticContact = null;
const el = (tag, text) => { const n = document.createElement(tag); n.textContent = text; return n; };
const table = (headers, rows) => {
  const t = document.createElement('table');
  const head = document.createElement('tr'); headers.forEach(h => head.appendChild(el('th', h))); t.appendChild(head);
  rows.forEach(r => { const tr = document.createElement('tr'); r.forEach(c => tr.appendChild(el('td', String(c ?? '—')))); t.appendChild(tr); });
  return t;
};
async function api(path, options) {
  const r = await fetch(path, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}
async function refresh() {
  try {
    const s = await api('/demo/api/state');
    const box = document.getElementById('state'); box.textContent = '';
    box.appendChild(el('p', 'Eventos del webhook: ' + JSON.stringify(s.events)));
    box.appendChild(el('p', 'Conversaciones: ' + JSON.stringify(s.conversations) + ' · Avisos a asesores: ' + JSON.stringify(s.notifications)));
    box.appendChild(el('h3', 'Prospectos'));
    box.appendChild(table(['Contacto', 'Etapa', 'Interés', 'Anuncio', 'Creado'], s.leads.map(l => [l.contact, l.status, l.interest, l.attribution, l.since])));
    box.appendChild(el('h3', 'Respuestas del agente (outbox)'));
    box.appendChild(table(['Tipo', 'Entrega', 'Texto', 'Fecha'], s.replies.map(r => [r.kind, r.delivery, r.text, r.at])));
    box.appendChild(el('h3', 'Transferencias'));
    box.appendChild(table(['Motivo', 'Prioridad', 'Estado', 'Abierta'], s.handoffs.map(h => [h.reason, h.priority, h.status, h.at])));
  } catch (e) { document.getElementById('state').textContent = 'No se pudo actualizar el estado.'; }
}
document.getElementById('send').addEventListener('click', async () => {
  const text = document.getElementById('text').value.trim(); if (!text) return;
  try {
    const r = await api('/demo/api/message', {method: 'POST', body: JSON.stringify({text, referral: document.getElementById('referral').checked, contact: syntheticContact})});
    syntheticContact = r.contact;
    document.getElementById('sent').textContent = 'Evento ' + r.accepted + ' · mismo contacto sintético en esta sesión: ' + r.contact + '. Procesado por el worker en breve.';
  } catch (e) { document.getElementById('sent').textContent = 'No se pudo enviar.'; }
  refresh();
});
document.getElementById('handoff').addEventListener('click', async () => {
  try {
    const s = await api('/demo/api/state');
    if (!s.open_conversations.length) { document.getElementById('handoffmsg').textContent = 'No hay conversación automatizada abierta.'; return; }
    await api('/demo/api/handoff', {method: 'POST', body: JSON.stringify({conversation_id: s.open_conversations[0].id})});
    document.getElementById('handoffmsg').textContent = 'Solicitud registrada. La automatización de esa conversación queda pausada.';
  } catch (e) { document.getElementById('handoffmsg').textContent = 'No se pudo transferir (requiere una conversación).'; }
  refresh();
});
refresh(); setInterval(refresh, 5000);
"""
