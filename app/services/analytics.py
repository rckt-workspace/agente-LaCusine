"""Analítica comercial semanal. Lógica pura: semanas en America/Bogota, tasas sin división por cero y comparativa.

Una semana comercial va de lunes 00:00 a lunes siguiente 00:00 (intervalo semiabierto), hora de Bogotá.
Las cifras no son personales: nunca incluyen números de teléfono, nombres ni contenido de mensajes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

BOGOTA = ZoneInfo("America/Bogota")
PERCENT_QUANT = Decimal("0.1")


@dataclass(frozen=True)
class WeekWindow:
    """Semana comercial. `start_day` es lunes; `end_day` es el lunes siguiente (exclusivo)."""

    start_day: date
    end_day: date

    @property
    def start_utc(self) -> datetime:
        return datetime.combine(self.start_day, time(0, 0), tzinfo=BOGOTA).astimezone(timezone.utc)

    @property
    def end_utc(self) -> datetime:
        return datetime.combine(self.end_day, time(0, 0), tzinfo=BOGOTA).astimezone(timezone.utc)


def week_containing(day: date) -> WeekWindow:
    """Semana que contiene `day` (fecha local de Bogotá)."""
    monday = day - timedelta(days=day.weekday())
    return WeekWindow(start_day=monday, end_day=monday + timedelta(days=7))


def previous_week(window: WeekWindow) -> WeekWindow:
    return week_containing(window.start_day - timedelta(days=1))


def local_day(moment: datetime) -> date:
    """Día de Bogotá de un instante con zona horaria. Un instante naive se rechaza: sería ambiguo."""
    if moment.tzinfo is None:
        raise ValueError("instante_sin_zona_horaria")
    return moment.astimezone(BOGOTA).date()


def rate(numerator: int, denominator: int) -> Decimal | None:
    """Proporción en porcentaje con una decimal. None si el denominador es cero: no hay base para calcular."""
    if denominator <= 0:
        return None
    return (Decimal(numerator) * 100 / Decimal(denominator)).quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def change_percent(current: int | Decimal, previous: int | Decimal) -> Decimal | None:
    """Variación porcentual frente a la semana anterior. None si la anterior es cero."""
    base = Decimal(previous)
    if base == 0:
        return None
    return ((Decimal(current) - base) * 100 / base).quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)
