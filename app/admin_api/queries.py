"""Modelo de lectura del panel. Solo SELECT sobre tablas existentes; ninguna métrica es fija ni simulada.

Datos personales: el número de WhatsApp sale enmascarado (solo los últimos 4 dígitos). Los textos de mensajes
se devuelven porque el asesor los necesita para atender; no se registran en logs.
Zona horaria de negocio: America/Bogota (UTC-5 fijo, sin horario de verano).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

BUSINESS_TZ = "America/Bogota"
MAX_PAGE = 100
MAX_ANALYTICS_DAYS = 90


def mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    return "•••• " + value[-4:]


def escape_like(term: str) -> str:
    """Escapa comodines para que la búsqueda sea literal. Se usa con ESCAPE '\\'."""
    cleaned = term.strip()[:80]
    escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class AdminReadModel:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    # --- Panel principal -----------------------------------------------------

    def dashboard(self, daily_budget_usd, monthly_budget_usd) -> dict[str, Any]:
        day_start = f"date_trunc('day', now() at time zone '{BUSINESS_TZ}') at time zone '{BUSINESS_TZ}'"
        month_start = f"date_trunc('month', now() at time zone '{BUSINESS_TZ}') at time zone '{BUSINESS_TZ}'"
        with self._pool.connection() as conn:
            conversations = dict(conn.execute(
                "select automation_state, count(*) from public.conversations where status <> 'closed' group by 1"
            ).fetchall())
            open_handoffs = conn.execute(
                "select count(*) from public.human_handoffs where status in ('open', 'assigned')"
            ).fetchone()[0]
            messages_today = dict(conn.execute(
                f"select direction, count(*) from public.messages where received_at >= {day_start} group by 1"
            ).fetchall())
            leads_by_status = dict(conn.execute(
                "select status, count(*) from public.leads group by 1"
            ).fetchall())
            leads_today = conn.execute(
                f"select count(*) from public.leads where created_at >= {day_start}"
            ).fetchone()[0]
            cost_today, cost_month, failed_today = conn.execute(
                f"select coalesce(sum(estimated_cost_usd) filter (where created_at >= {day_start}), 0), "
                f"coalesce(sum(estimated_cost_usd) filter (where created_at >= {month_start}), 0), "
                f"count(*) filter (where status = 'failed' and created_at >= {day_start}) "
                "from public.llm_usage_events where status in ('succeeded', 'failed')"
            ).fetchone()
            recent = conn.execute(
                "select c.id, c.automation_state, c.last_activity_at, l.wa_contact_id, l.display_name, l.status "
                "from public.conversations c join public.leads l on l.id = c.lead_id "
                "order by c.last_activity_at desc limit 5"
            ).fetchall()

        return {
            "conversations": {
                "automated": int(conversations.get("automated", 0)),
                "paused_for_human": int(conversations.get("paused_for_human", 0)),
                "disabled": int(conversations.get("disabled", 0)),
            },
            "open_handoffs": int(open_handoffs),
            "messages_today": {"inbound": int(messages_today.get("inbound", 0)),
                               "outbound": int(messages_today.get("outbound", 0))},
            "leads_today": int(leads_today),
            "leads_by_status": {str(k): int(v) for k, v in leads_by_status.items()},
            "llm": {
                "cost_today_usd": str(cost_today),
                "cost_month_usd": str(cost_month),
                "failed_calls_today": int(failed_today),
                "daily_budget_usd": str(daily_budget_usd) if daily_budget_usd is not None else None,
                "monthly_budget_usd": str(monthly_budget_usd) if monthly_budget_usd is not None else None,
            },
            "recent_conversations": [
                {
                    "id": str(row[0]), "automation_state": row[1], "last_activity_at": row[2].isoformat(),
                    "contact": mask_phone(row[3]), "display_name": row[4], "lead_status": row[5],
                }
                for row in recent
            ],
        }

    # --- Conversaciones --------------------------------------------------------

    def list_conversations(self, *, state: str | None, status: str | None, q: str | None,
                           limit: int, offset: int) -> dict[str, Any]:
        params = {
            "state": state, "status": status,
            "q": escape_like(q) if q and q.strip() else None,
            "limit": _page(limit), "offset": max(offset, 0),
        }
        where = (
            "(%(state)s::text is null or c.automation_state = %(state)s) and "
            "(%(status)s::text is null or c.status = %(status)s) and "
            "(%(q)s::text is null or l.display_name ilike %(q)s escape '\\' or l.wa_contact_id like %(q)s escape '\\')"
        )
        with self._pool.connection() as conn:
            total = conn.execute(
                f"select count(*) from public.conversations c join public.leads l on l.id = c.lead_id where {where}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                "select c.id, c.status, c.automation_state, c.started_at, c.last_activity_at, "
                "l.wa_contact_id, l.display_name, l.status, l.product_interest, "
                "(select count(*) from public.human_handoffs h "
                " where h.conversation_id = c.id and h.status in ('open', 'assigned')) "
                "from public.conversations c join public.leads l on l.id = c.lead_id "
                f"where {where} order by c.last_activity_at desc limit %(limit)s offset %(offset)s",
                params,
            ).fetchall()
        items = [
            {
                "id": str(r[0]), "status": r[1], "automation_state": r[2], "started_at": r[3].isoformat(),
                "last_activity_at": r[4].isoformat(), "contact": mask_phone(r[5]), "display_name": r[6],
                "lead_status": r[7], "product_interest": r[8], "open_handoffs": int(r[9]),
            }
            for r in rows
        ]
        return {"items": items, "total": int(total), "limit": params["limit"], "offset": params["offset"]}

    def get_conversation(self, conversation_id: UUID) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            head = conn.execute(
                "select c.id, c.status, c.automation_state, c.started_at, c.last_activity_at, "
                "l.wa_contact_id, l.display_name, l.status, l.city, l.product_interest, l.budget_cop "
                "from public.conversations c join public.leads l on l.id = c.lead_id where c.id = %s",
                (conversation_id,),
            ).fetchone()
            if head is None:
                return None
            messages = conn.execute(
                "select direction, message_type, processing_status, content_text, received_at "
                "from public.messages where conversation_id = %s order by received_at desc limit 100",
                (conversation_id,),
            ).fetchall()
            handoffs = conn.execute(
                "select id, reason, status, priority, assigned_advisor, opened_at, closed_at "
                "from public.human_handoffs where conversation_id = %s order by opened_at desc",
                (conversation_id,),
            ).fetchall()
        return {
            "id": str(head[0]), "status": head[1], "automation_state": head[2],
            "started_at": head[3].isoformat(), "last_activity_at": head[4].isoformat(),
            "lead": {
                "contact": mask_phone(head[5]), "display_name": head[6], "status": head[7],
                "city": head[8], "product_interest": head[9], "budget_cop": head[10],
            },
            # Se muestran en orden cronológico ascendente para leer la conversación.
            "messages": [
                {"direction": m[0], "message_type": m[1], "processing_status": m[2],
                 "content_text": m[3], "received_at": m[4].isoformat()}
                for m in reversed(messages)
            ],
            "handoffs": [
                {"id": str(h[0]), "reason": h[1], "status": h[2], "priority": h[3], "assigned_advisor": h[4],
                 "opened_at": h[5].isoformat(), "closed_at": h[6].isoformat() if h[6] else None}
                for h in handoffs
            ],
        }

    # --- Leads, solicitudes y catálogo ----------------------------------------

    def list_leads(self, *, status: str | None, q: str | None, limit: int, offset: int) -> dict[str, Any]:
        params = {
            "status": status, "q": escape_like(q) if q and q.strip() else None,
            "limit": _page(limit), "offset": max(offset, 0),
        }
        where = (
            "(%(status)s::text is null or status = %(status)s) and "
            "(%(q)s::text is null or display_name ilike %(q)s escape '\\' or wa_contact_id like %(q)s escape '\\')"
        )
        with self._pool.connection() as conn:
            total = conn.execute(f"select count(*) from public.leads where {where}", params).fetchone()[0]
            rows = conn.execute(
                "select id, wa_contact_id, display_name, city, product_interest, budget_cop, status, "
                "acquisition_source, created_at, updated_at from public.leads "
                f"where {where} order by updated_at desc limit %(limit)s offset %(offset)s",
                params,
            ).fetchall()
        items = [
            {"id": str(r[0]), "contact": mask_phone(r[1]), "display_name": r[2], "city": r[3],
             "product_interest": r[4], "budget_cop": r[5], "status": r[6], "acquisition_source": r[7],
             "created_at": r[8].isoformat(), "updated_at": r[9].isoformat()}
            for r in rows
        ]
        return {"items": items, "total": int(total), "limit": params["limit"], "offset": params["offset"]}

    def list_handoffs(self, *, status: str | None, limit: int, offset: int) -> dict[str, Any]:
        params = {"status": status, "limit": _page(limit), "offset": max(offset, 0)}
        where = "(%(status)s::text is null or h.status = %(status)s)"
        with self._pool.connection() as conn:
            total = conn.execute(
                f"select count(*) from public.human_handoffs h where {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                "select h.id, h.conversation_id, h.reason, h.status, h.priority, h.assigned_advisor, h.opened_at, "
                "l.wa_contact_id, l.display_name "
                "from public.human_handoffs h join public.conversations c on c.id = h.conversation_id "
                "join public.leads l on l.id = c.lead_id "
                f"where {where} order by h.opened_at desc limit %(limit)s offset %(offset)s",
                params,
            ).fetchall()
        items = [
            {"id": str(r[0]), "conversation_id": str(r[1]), "reason": r[2], "status": r[3], "priority": r[4],
             "assigned_advisor": r[5], "opened_at": r[6].isoformat(), "contact": mask_phone(r[7]),
             "display_name": r[8]}
            for r in rows
        ]
        return {"items": items, "total": int(total), "limit": params["limit"], "offset": params["offset"]}

    def list_products(self, *, q: str | None, limit: int, offset: int) -> dict[str, Any]:
        params = {"q": escape_like(q) if q and q.strip() else None, "limit": _page(limit), "offset": max(offset, 0)}
        where = (
            "(%(q)s::text is null or reference ilike %(q)s escape '\\' or brand ilike %(q)s escape '\\' "
            "or category ilike %(q)s escape '\\')"
        )
        with self._pool.connection() as conn:
            total = conn.execute(f"select count(*) from public.products where {where}", params).fetchone()[0]
            rows = conn.execute(
                "select id, reference, brand, category, price_cop, availability_label, availability_as_of, "
                "product_url, updated_at from public.products "
                f"where {where} order by brand, reference limit %(limit)s offset %(offset)s",
                params,
            ).fetchall()
        items = [
            {"id": str(r[0]), "reference": r[1], "brand": r[2], "category": r[3],
             "price_cop": str(r[4]) if r[4] is not None else None, "availability_label": r[5],
             "availability_as_of": r[6].isoformat() if r[6] else None, "product_url": r[7],
             "updated_at": r[8].isoformat()}
            for r in rows
        ]
        return {"items": items, "total": int(total), "limit": params["limit"], "offset": params["offset"]}

    # --- Analítica -------------------------------------------------------------

    def analytics(self, days: int) -> dict[str, Any]:
        days = min(max(days, 1), MAX_ANALYTICS_DAYS)
        with self._pool.connection() as conn:
            daily = conn.execute(
                f"select (received_at at time zone '{BUSINESS_TZ}')::date as day, direction, count(*) "
                "from public.messages where received_at >= now() - make_interval(days => %s) "
                "group by 1, 2 order by 1",
                (days,),
            ).fetchall()
            conversations_started = conn.execute(
                "select count(*) from public.conversations where started_at >= now() - make_interval(days => %s)",
                (days,),
            ).fetchone()[0]
            handoffs_by_reason = conn.execute(
                "select reason, count(*) from public.human_handoffs "
                "where opened_at >= now() - make_interval(days => %s) group by 1 order by 2 desc",
                (days,),
            ).fetchall()
            leads_by_source = conn.execute(
                "select coalesce(acquisition_source, 'sin origen'), count(*) from public.leads "
                "where created_at >= now() - make_interval(days => %s) group by 1 order by 2 desc limit 10",
                (days,),
            ).fetchall()
        series: dict[str, dict[str, int]] = {}
        for day, direction, count in daily:
            series.setdefault(day.isoformat(), {"inbound": 0, "outbound": 0})[direction] = int(count)
        return {
            "days": days,
            "messages_by_day": [{"day": k, **v} for k, v in sorted(series.items())],
            "conversations_started": int(conversations_started),
            "handoffs_by_reason": [{"reason": r, "count": int(c)} for r, c in handoffs_by_reason],
            "leads_by_source": [{"source": s, "count": int(c)} for s, c in leads_by_source],
        }


def _page(limit: int) -> int:
    return min(max(int(limit), 1), MAX_PAGE)
