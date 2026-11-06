"""Lectura agregada para la analítica semanal. Solo SELECT, consultas parametrizadas y sin datos personales.

Fuentes:
  * Filas vivas de las tablas operativas (dentro del rango de la semana).
  * `analytics_daily_counts`: conteos consolidados antes de la retención. Sus días son de Bogotá; como la semana
    también empieza en lunes de Bogotá, la suma por días coincide exactamente con el rango de la semana.
Ambas fuentes son disjuntas: una fila consolidada ya no existe en su tabla viva.
"""

from __future__ import annotations

from decimal import Decimal

from psycopg_pool import ConnectionPool, PoolClosed
import psycopg

from app.repositories.errors import StorageError
from app.services.analytics import WeekWindow


class PostgresAnalyticsRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def weekly_counts(self, window: WeekWindow) -> dict:
        start, end = window.start_utc, window.end_utc
        d0, d1 = window.start_day, window.end_day
        try:
            with self._pool.connection() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                inbound = _scalar(conn, """
                    select count(*) from public.messages
                    where direction = 'inbound' and received_at >= %s and received_at < %s
                """, (start, end))
                outbound = _merge(_pairs(conn, """
                    select delivery_status, count(*) from public.outbound_messages
                    where created_at >= %s and created_at < %s group by 1
                """, (start, end)), _rolled(conn, "outbound:", d0, d1))
                webhook = _merge(_pairs(conn, """
                    select processing_status, count(*) from public.webhook_events
                    where received_at >= %s and received_at < %s group by 1
                """, (start, end)), _rolled(conn, "webhook_events:", d0, d1))
                conversations_new = _scalar(conn, """
                    select count(*) from public.conversations
                    where started_at >= %s and started_at < %s
                """, (start, end))
                handoffs = dict(_pairs(conn, """
                    select reason, count(*) from public.human_handoffs
                    where opened_at >= %s and opened_at < %s group by 1
                """, (start, end)))
                leads_by_status = dict(_pairs(conn, """
                    select status, count(*) from public.leads
                    where created_at >= %s and created_at < %s group by 1
                """, (start, end)))
                llm = conn.execute("""
                    select
                      count(*) filter (where u.status = 'succeeded'),
                      count(*) filter (where u.status = 'failed'),
                      coalesce(sum(u.input_tokens) filter (where u.status = 'succeeded'), 0),
                      coalesce(sum(u.output_tokens) filter (where u.status = 'succeeded'), 0),
                      coalesce(sum(u.cache_read_tokens) filter (where u.status = 'succeeded'), 0),
                      coalesce(sum(u.cache_write_tokens) filter (where u.status = 'succeeded'), 0),
                      coalesce(sum(u.estimated_cost_usd) filter (where u.status = 'succeeded'), 0),
                      count(distinct u.conversation_id) filter (where u.status = 'succeeded'),
                      count(distinct c.lead_id) filter (where u.status = 'succeeded')
                    from public.llm_usage_events u
                    left join public.conversations c on c.id = u.conversation_id
                    where u.created_at >= %s and u.created_at < %s
                """, (start, end)).fetchone()
                llm_failures = dict(_pairs(conn, """
                    select error_code, count(*) from public.llm_usage_events
                    where status = 'failed' and created_at >= %s and created_at < %s group by 1
                """, (start, end)))
                reservations = dict(_pairs(conn, """
                    select status, count(*) from public.budget_reservations
                    where created_at >= %s and created_at < %s group by 1
                """, (start, end)))
                lead_sources = dict(_pairs(conn, """
                    select coalesce(referral_source_type, 'sin_informacion'), count(*) from public.leads
                    where created_at >= %s and created_at < %s group by 1
                """, (start, end)))
                commercial = {
                    "events_by_type": {str(k): (int(c), int(l)) for k, c, l in conn.execute("""
                        select event_type, count(*), count(distinct lead_id) from public.commercial_events
                        where occurred_at >= %s and occurred_at < %s group by 1
                    """, (start, end)).fetchall()},
                    "events_total_ever": _scalar(conn, "select count(*) from public.commercial_events", ()),
                    "sales_total_ever": _scalar(conn, "select count(*) from public.sales", ()),
                    "top_product_interest": [
                        (str(brand), str(category), int(n)) for brand, category, n in conn.execute("""
                            select p.brand, p.category, count(*) from public.commercial_events e
                            join public.products p on p.id = e.product_id
                            where e.event_type = 'product_interest' and e.occurred_at >= %s and e.occurred_at < %s
                            group by 1, 2 order by 3 desc, 1, 2 limit 10
                        """, (start, end)).fetchall()],
                    # Ventas por fecha de venta, con su estado actual derivado del último evento de estado.
                    "sales_in_week": conn.execute("""
                        with s as (
                          select s.id, s.amount_cop,
                            (select e.status from public.sale_status_events e where e.sale_id = s.id
                             order by e.occurred_at desc, e.id desc limit 1) as status
                          from public.sales s where s.sold_at >= %s and s.sold_at < %s)
                        select count(*) filter (where status = 'confirmed'),
                               coalesce(sum(amount_cop) filter (where status = 'confirmed'), 0),
                               count(*) filter (where status = 'cancelled')
                        from s
                    """, (start, end)).fetchone(),
                    # Cohorte: leads creados en la semana y cuántos tienen alguna venta confirmada vigente.
                    "lead_cohort": conn.execute("""
                        with cohort as (select id from public.leads where created_at >= %s and created_at < %s)
                        select (select count(*) from cohort),
                               (select count(*) from cohort c where exists (
                                  select 1 from public.sales s where s.lead_id = c.id
                                  and (select e.status from public.sale_status_events e where e.sale_id = s.id
                                       order by e.occurred_at desc, e.id desc limit 1) = 'confirmed'))
                    """, (start, end)).fetchone(),
                }
        except (psycopg.Error, PoolClosed):
            raise StorageError("analytics_read_failed") from None

        return {
            "inbound_messages": inbound,
            "outbound": dict(outbound),
            "webhook_events": dict(webhook),
            "conversations_new": conversations_new,
            "handoffs_by_reason": handoffs,
            "leads_created_by_status": leads_by_status,
            "llm": {
                "calls_succeeded": int(llm[0]),
                "calls_failed": int(llm[1]),
                "input_tokens": int(llm[2]),
                "output_tokens": int(llm[3]),
                "cache_read_tokens": int(llm[4]),
                "cache_write_tokens": int(llm[5]),
                "cost_usd": Decimal(llm[6]),
                "conversations_with_ai": int(llm[7]),
                "leads_with_ai": int(llm[8]),
            },
            "llm_failures_by_code": llm_failures,
            "budget_reservations_by_status": reservations,
            "commercial": commercial,
            "lead_sources": lead_sources,
        }


def _merge(*pair_lists) -> dict:
    """Suma por clave. Cada lista aporta pares (clave, valor); las fuentes son disjuntas."""
    total: dict[str, int] = {}
    for pairs in pair_lists:
        for key, value in pairs:
            total[key] = total.get(key, 0) + value
    return total


def _scalar(conn, sql: str, params: tuple) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _pairs(conn, sql: str, params: tuple) -> list[tuple[str, int]]:
    return [(str(key), int(value)) for key, value in conn.execute(sql, params).fetchall()]


def _rolled(conn, prefix: str, start_day, end_day) -> list[tuple[str, int]]:
    rows = conn.execute("""
        select metric, sum(value) from public.analytics_daily_counts
        where metric like %s and day >= %s and day < %s group by metric
    """, (prefix + "%", start_day, end_day)).fetchall()
    return [(metric[len(prefix):], int(total)) for metric, total in rows]
