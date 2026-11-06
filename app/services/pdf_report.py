"""Generación de informes PDF empresariales. Solo presenta cifras ya calculadas por el servicio de analítica.

Reglas:
  * No calcula ni inventa indicadores: cada cifra viene del informe semanal verificado.
  * Sin datos se declara como "Sin datos suficientes" o "Sin información disponible"; un cero real se muestra como 0.
  * El texto narrativo (opcional, de un modelo) se incluye tal cual en su sección, separado de las cifras.
  * Los documentos no guardan datos personales: el informe no contiene teléfonos ni contenido de conversaciones.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.analytics import BOGOTA

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_TABLE_ROWS = 60
BRAND_DARK = colors.HexColor("#1e293b")
BRAND_ACCENT = colors.HexColor("#4f46e5")
BRAND_MUTED = colors.HexColor("#64748b")
ZEBRA = colors.HexColor("#f1f5f9")

KINDS = {
    "operativo": "Informe operativo semanal",
    "marketing": "Informe de marketing y captación",
    "oportunidades": "Informe comercial y de oportunidades",
    "ventas": "Informe de ventas confirmadas",
    "ejecutivo": "Informe ejecutivo consolidado",
}
COMPARISON_LABELS = {
    "mensajes_entrantes_pct": "Mensajes recibidos",
    "respuestas_aceptadas_pct": "Respuestas aceptadas por Meta",
    "conversaciones_nuevas_pct": "Conversaciones nuevas",
    "costo_estimado_pct": "Costo estimado de IA",
}
NO_DATA = "Sin datos suficientes"
NO_SOURCE = "Sin información disponible"


class PdfTooLargeError(Exception):
    code = "pdf_too_large"


def cop(value) -> str:
    if value is None:
        return NO_DATA
    return "$" + f"{int(value):,}".replace(",", ".") + " COP"


def count(value) -> str:
    return NO_DATA if value is None else f"{int(value):,}".replace(",", ".")


def percent(value) -> str:
    return NO_DATA if value is None else f"{str(value).replace('.', ',')} %"


def usd(value) -> str:
    if value is None:
        return NO_DATA
    amount = Decimal(str(value))
    text = f"{amount:.2f}" if abs(amount) >= 1 else f"{amount:.4f}"
    return text.replace(".", ",") + " USD"


MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
             "octubre", "noviembre", "diciembre"]


def _date_es(iso: str) -> str:
    day = datetime.fromisoformat(iso)
    return f"{day.day} de {MONTHS_ES[day.month - 1]} de {day.year}"


def _period_text(start_iso: str, end_exclusive_iso: str) -> str:
    """Periodo legible e inclusivo: el límite de fin es exclusivo, así que el último día es el anterior."""
    start = datetime.fromisoformat(start_iso)
    last = datetime.fromisoformat(end_exclusive_iso) - timedelta(days=1)
    if start.year == last.year and start.month == last.month:
        return f"del {start.day} al {last.day} de {MONTHS_ES[last.month - 1]} de {last.year}"
    return f"del {_date_es(start_iso)} al {_date_es(last.date().isoformat())}"


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold", fontSize=26, leading=30,
                                textColor=BRAND_DARK, alignment=0, spaceAfter=10),
        "subtitle": ParagraphStyle("st", parent=base["Normal"], fontName="Helvetica", fontSize=13, leading=17,
                                   textColor=BRAND_MUTED, spaceAfter=6),
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=19,
                             textColor=BRAND_ACCENT, spaceBefore=10, spaceAfter=6, keepWithNext=1),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, leading=15,
                             textColor=BRAND_DARK, spaceBefore=8, spaceAfter=4, keepWithNext=1),
        "body": ParagraphStyle("b", parent=base["Normal"], fontName="Helvetica", fontSize=10, leading=14,
                               textColor=BRAND_DARK, spaceAfter=4),
        "bullet": ParagraphStyle("bl", parent=base["Normal"], fontName="Helvetica", fontSize=10, leading=14,
                                 leftIndent=12, bulletIndent=2, textColor=BRAND_DARK, spaceAfter=3),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontName="Helvetica", fontSize=9, leading=11.5),
        "cell_head": ParagraphStyle("ch", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9, leading=11.5,
                                    textColor=colors.white),
        "note": ParagraphStyle("n", parent=base["Normal"], fontName="Helvetica-Oblique", fontSize=8.5, leading=11,
                               textColor=BRAND_MUTED, spaceAfter=4),
    }


def _p(text, style):
    return Paragraph(escape(str(text)), style)


class _NumberedCanvas(rl_canvas.Canvas):
    """Dibuja 'Página X de Y' cuando ya se conoce el total de páginas."""

    def __init__(self, *args, footer_text: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []
        self._footer_text = footer_text

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._draw_footer(total)
            # Llamada a la clase base: la subclase guardaría de nuevo el estado y duplicaría páginas.
            rl_canvas.Canvas.showPage(self)
        rl_canvas.Canvas.save(self)

    def _draw_footer(self, total):
        self.setFont("Helvetica", 8)
        self.setFillColor(BRAND_MUTED)
        self.drawString(1.8 * cm, 1.1 * cm, self._footer_text)
        self.drawRightString(A4[0] - 1.8 * cm, 1.1 * cm, f"Página {self._pageNumber} de {total}")
        self.setStrokeColor(BRAND_ACCENT)
        self.setLineWidth(0.6)
        self.line(1.8 * cm, 1.5 * cm, A4[0] - 1.8 * cm, 1.5 * cm)


def _table(headers, rows, widths, st):
    data = [[Paragraph(escape(h), st["cell_head"]) for h in headers]]
    for row in rows[:MAX_TABLE_ROWS]:
        data.append([Paragraph(escape(str(v)), st["cell"]) for v in row])
    table = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for index in range(1, len(data)):
        if index % 2 == 0:
            style.append(("BACKGROUND", (0, index), (-1, index), ZEBRA))
    table.setStyle(TableStyle(style))
    return table


def _kpi_table(items, st):
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    flat = []
    for row in rows:
        cells = []
        for label, value in row:
            cells.append(Paragraph(f'<font size=8 color="#64748b">{escape(label)}</font><br/>'
                                   f'<font size=13><b>{escape(str(value))}</b></font>', st["body"]))
        while len(cells) < 2:
            cells.append("")
        flat.append(cells)
    table = Table(flat, colWidths=[8.2 * cm, 8.2 * cm])
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _bar_chart(labels, values, title, *, horizontal=False):
    """Gráfico de barras. Solo se llama con valores disponibles; los ceros son ceros reales."""
    drawing = Drawing(16 * cm, 6.2 * cm)
    chart = HorizontalBarChart() if horizontal else VerticalBarChart()
    chart.x, chart.y = 2.4 * cm, 1.0 * cm
    chart.width, chart.height = 12.8 * cm, 4.2 * cm
    chart.data = [list(values)]
    chart.categoryAxis.categoryNames = list(labels)
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 7.5
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7.5
    chart.valueAxis.valueMin = 0
    top = max(values) if values else 0
    chart.valueAxis.valueMax = max(top * 1.15, 1)  # escala entera y legible, nunca decimales diminutos
    chart.valueAxis.valueStep = max(1, int(chart.valueAxis.valueMax // 5) or 1)
    chart.bars[0].fillColor = BRAND_ACCENT
    drawing.add(chart)
    from reportlab.graphics.shapes import String
    drawing.add(String(2.4 * cm, 5.7 * cm, title, fontName="Helvetica-Bold", fontSize=9, fillColor=BRAND_DARK))
    return drawing


def _sales_block(report, st):
    sales = report["ventas"]
    if sales["estado"] != "disponible":
        return [_p(f"{NO_DATA}: no hay ventas confirmadas registradas en el periodo. No se estiman ingresos.", st["body"])]
    return [_kpi_table([
        ("Ventas confirmadas", count(sales["ventas_confirmadas"])),
        ("Ingresos confirmados", cop(sales["valor_confirmado_cop"])),
        ("Ticket promedio", cop(sales["ticket_promedio_cop"])),
        ("Ventas canceladas", count(sales["ventas_canceladas"])),
        ("Conversión lead a venta", percent(sales["conversion_lead_a_venta_pct"])),
        ("Leads de la cohorte", count(sales["leads_de_la_cohorte"])),
    ], st)]


def _cover(story, st, title, period, generated):
    story.append(Spacer(1, 4.5 * cm))
    story.append(_p("LA CUISINE APPLIANCES COLOMBIA", st["subtitle"]))
    story.append(_p(title, st["title"]))
    story.append(_p(f"Periodo analizado: {period}", st["subtitle"]))
    story.append(_p(f"Generado: {generated}", st["subtitle"]))
    story.append(Spacer(1, 1.2 * cm))
    story.append(_p("Documento confidencial de uso interno. Las cifras provienen de la base de datos del "
                    "agente comercial y se calculan con zona horaria America/Bogota.", st["note"]))
    story.append(PageBreak())


def _operativo(report, st, story):
    act, ia = report["actividad"], report["operacion_ia"]
    story.append(_p("1. Actividad del agente", st["h1"]))
    story.append(_kpi_table([
        ("Mensajes recibidos", count(act["mensajes_entrantes"])),
        ("Respuestas aceptadas por Meta", count(act["respuestas_aceptadas_por_meta"])),
        ("Respuestas entregadas o leídas", count(act["respuestas_entregadas_o_leidas"])),
        ("Tasa de entrega", percent(act["tasa_entrega_pct"])),
        ("Conversaciones nuevas", count(act["conversaciones_nuevas"])),
        ("Derivaciones a asesor", count(act["conversaciones_derivadas_a_asesor"])),
    ], st))
    story.append(Spacer(1, 6))
    story.append(_p("2. Errores y entregas", st["h1"]))
    story.append(_p(f"Consultas fallidas: {count(act['consultas_fallidas'])}. Respuestas fallidas: "
                    f"{count(act['respuestas_fallidas'])}. Pendientes: {count(act['respuestas_pendientes'])}. "
                    f"Inciertas (Meta pudo aceptarlas): {count(act['respuestas_inciertas'])}.", st["body"]))
    if act["detalle_fallos_ia"]:
        rows = [(code, count(total)) for code, total in act["detalle_fallos_ia"].items()]
        story.append(_table(["Código de fallo de IA", "Casos"], rows, [11 * cm, 4 * cm], st))
    story.append(_p("3. Consumo del agente", st["h1"]))
    story.append(_kpi_table([
        ("Llamadas exitosas", count(ia["llamadas_exitosas"])),
        ("Llamadas fallidas", count(ia["llamadas_fallidas"])),
        ("Costo estimado", usd(ia["costo_estimado_usd"])),
        ("Costo por conversación", usd(ia["costo_por_conversacion_usd"])),
    ], st))
    comparison = report.get("comparacion_semana_anterior")
    if comparison:
        story.append(_p("4. Comparación con la semana anterior", st["h1"]))
        rows = [(COMPARISON_LABELS.get(k, k), percent(v)) for k, v in comparison.items()]
        story.append(_table(["Indicador", "Variación"], rows, [11 * cm, 4 * cm], st))


def _marketing(report, st, story, trend):
    prospects = report["prospectos"]
    story.append(_p("1. Captación de prospectos", st["h1"]))
    story.append(_kpi_table([
        ("Leads nuevos", count(prospects["leads_nuevos"])),
        ("Leads calificados", count(prospects["leads_calificados"])),
    ], st))
    origin = prospects["origen_prospectos"]
    story.append(_p("2. Fuentes de captación", st["h1"]))
    if origin["estado"] == "sin_informacion":
        story.append(_p(f"{NO_SOURCE}. Ningún prospecto tiene origen de anuncio capturado en el periodo; "
                        "no se atribuye ninguna campaña.", st["body"]))
    elif origin["estado"] == "sin_leads":
        story.append(_p("No hay prospectos nuevos en el periodo.", st["body"]))
    else:
        rows = [(k, count(v)) for k, v in origin["detalle"].items()]
        story.append(_table(["Origen", "Prospectos"], rows, [11 * cm, 4 * cm], st))
    story.append(_p("3. Categorías y marcas consultadas", st["h1"]))
    top = prospects["productos_mas_consultados"]
    if top is None:
        story.append(_p(NO_DATA + " de interés por producto.", st["body"]))
    elif not top:
        story.append(_p("Sin consultas por producto en el periodo.", st["body"]))
    else:
        story.append(_table(["Marca", "Categoría", "Consultas"],
                            [(p["marca"], p["categoria"], count(p["consultas"])) for p in top],
                            [4.5 * cm, 8 * cm, 2.5 * cm], st))
    if trend:
        story.append(_p("4. Evolución semanal", st["h1"]))
        story.append(_bar_chart([t["semana_inicio"][5:] for t in trend],
                                [t["mensajes_entrantes"] for t in trend], "Mensajes recibidos por semana"))


def _opportunities(report, st, story):
    prospects = report["prospectos"]
    story.append(_p("1. Señales comerciales", st["h1"]))
    story.append(_kpi_table([
        ("Leads con intención de compra", count(prospects["leads_con_intencion_de_compra"])),
        ("Cotizaciones solicitadas", count(prospects["leads_con_cotizacion_solicitada"])),
        ("Leads calificados", count(prospects["leads_calificados"])),
        ("Transferidos a asesor", count(prospects["leads_transferidos_a_asesor"])),
    ], st))
    story.append(_p("La intención de compra es una señal, no una venta. Solo las ventas confirmadas "
                    "cuentan como ingreso.", st["note"]))
    story.append(_p("2. Motivos de derivación", st["h1"]))
    reasons = prospects["derivaciones_por_motivo"]
    if not reasons:
        story.append(_p("Sin derivaciones a asesor en el periodo.", st["body"]))
    else:
        story.append(_table(["Motivo", "Casos"], [(k, count(v)) for k, v in reasons.items()], [11 * cm, 4 * cm], st))


def _sales(report, st, story):
    story.append(_p("1. Resultados de ventas", st["h1"]))
    story.extend(_sales_block(report, st))
    story.append(_p("2. Lectura de los datos", st["h1"]))
    sales = report["ventas"]
    if sales["estado"] != "disponible":
        story.append(_p("No hay historia comercial registrada. Este informe no puede medir ventas hasta que el "
                        "equipo confirme las primeras ventas con evidencia.", st["body"]))
    else:
        story.append(_p(f"Ventas canceladas en el periodo: {count(sales['ventas_canceladas'])}. Las cancelaciones "
                        "no suman a los ingresos.", st["body"]))


def _funnel(report, st, story):
    prospects, sales = report["prospectos"], report["ventas"]
    stages = [("Leads nuevos", prospects["leads_nuevos"]), ("Calificados", prospects["leads_calificados"]),
              ("Intención de compra", prospects["leads_con_intencion_de_compra"]),
              ("Cotización", prospects["leads_con_cotizacion_solicitada"]),
              ("Ventas confirmadas", sales["ventas_confirmadas"])]
    known = [(label, value) for label, value in stages if value is not None]
    if not known:
        story.append(_p(NO_DATA + " para el embudo.", st["body"]))
        return
    if all(int(value) == 0 for _, value in known):
        story.append(_p("Sin actividad comercial registrada en el periodo: todas las etapas están en cero.", st["body"]))
        return
    story.append(_bar_chart([label for label, _ in known], [int(v) for _, v in known],
                            "Embudo comercial (cifras del periodo)", horizontal=True))


def _executive(report, st, story, trend):
    story.append(_p("1. Resumen ejecutivo", st["h1"]))
    for line in report["resumen_ejecutivo"]:
        story.append(Paragraph(escape(line), st["bullet"], bulletText="•"))
    story.append(_p("2. Indicadores clave", st["h1"]))
    story.append(_kpi_table([
        ("Mensajes recibidos", count(report["actividad"]["mensajes_entrantes"])),
        ("Tasa de entrega", percent(report["actividad"]["tasa_entrega_pct"])),
        ("Ventas confirmadas", count(report["ventas"]["ventas_confirmadas"])),
        ("Ingresos confirmados", cop(report["ventas"]["valor_confirmado_cop"])),
        ("Costo de IA", usd(report["operacion_ia"]["costo_estimado_usd"])),
        ("Conversión lead a venta", percent(report["ventas"]["conversion_lead_a_venta_pct"])),
    ], st))
    story.append(_p("3. Embudo", st["h1"]))
    _funnel(report, st, story)
    story.append(_p("4. Hallazgos", st["h1"]))
    for line in _findings(report):
        story.append(Paragraph(escape(line), st["bullet"], bulletText="•"))
    story.append(_p("5. Recomendaciones", st["h1"]))
    for line in _recommendations(report):
        story.append(Paragraph(escape(line), st["bullet"], bulletText="•"))


def _findings(report):
    lines = []
    rate = report["actividad"]["tasa_entrega_pct"]
    if rate is not None and rate < 80:
        lines.append(f"La tasa de entrega es de {percent(rate)}: parte de las respuestas aún no llega al cliente.")
    if report["ventas"]["estado"] != "disponible":
        lines.append("No hay ventas confirmadas registradas: no se puede medir ingreso ni conversión.")
    if report["actividad"]["respuestas_inciertas"]:
        lines.append(f"Hay {report['actividad']['respuestas_inciertas']} respuesta(s) inciertas que requieren revisión.")
    if report["operacion_ia"]["llamadas_fallidas"]:
        lines.append(f"Se registraron {report['operacion_ia']['llamadas_fallidas']} llamadas de IA fallidas.")
    return lines or ["No se identifican alertas operativas en el periodo con los datos disponibles."]


def _recommendations(report):
    lines = []
    if report["ventas"]["estado"] != "disponible":
        lines.append("Registrar las primeras ventas confirmadas con evidencia para habilitar ingreso y conversión.")
    if report["prospectos"]["origen_prospectos"]["estado"] == "sin_informacion":
        lines.append("Verificar la captura del origen de los anuncios para atribuir prospectos a campañas reales.")
    if report["actividad"]["respuestas_inciertas"]:
        lines.append("Conciliar las respuestas inciertas antes de reenviar cualquier mensaje.")
    return lines or ["Mantener el seguimiento semanal y comparar con el periodo anterior."]


def _narrative(story, st, narrative):
    if narrative:
        story.append(_p("6. Análisis narrativo (asistido por IA)", st["h1"]))
        story.append(_p("Interpretación generada a partir de los indicadores de este informe. Las cifras no se "
                        "modifican; revísela antes de decidir.", st["note"]))
        for line in narrative:
            story.append(Paragraph(escape(line), st["bullet"], bulletText="•"))


def _limitations(report, st, story):
    story.append(_p("Limitaciones de los datos", st["h1"]))
    items = [
        "Las ventas solo cuentan cuando un administrador las confirma con evidencia.",
        "La intención de compra es una señal del agente, no una venta.",
        "Los bloqueos por presupuesto no se guardan en la base; solo quedan en el registro del servidor.",
    ]
    if report["prospectos"]["origen_prospectos"]["estado"] == "sin_informacion":
        items.append("No hay origen de anuncio capturado: la atribución por campaña no está disponible.")
    for item in items:
        story.append(Paragraph(escape(item), st["bullet"], bulletText="•"))


def build_pdf(*, kind: str, report: dict, generated_at: datetime, trend: list | None = None,
              narrative: list[str] | None = None) -> bytes:
    if kind not in KINDS:
        raise ValueError("report_kind_unknown")
    st = _styles()
    title = KINDS[kind]
    period = _period_text(report["semana"]["inicio_bogota"], report["semana"]["fin_exclusivo_bogota"])
    generated = generated_at.astimezone(BOGOTA).strftime("%d/%m/%Y %H:%M")
    story: list = []
    _cover(story, st, title, period, generated)
    if kind == "operativo":
        _operativo(report, st, story)
    elif kind == "marketing":
        _marketing(report, st, story, trend or [])
    elif kind == "oportunidades":
        _opportunities(report, st, story)
        story.append(_p("2b. Embudo", st["h1"]))
        _funnel(report, st, story)
    elif kind == "ventas":
        _sales(report, st, story)
    else:
        _executive(report, st, story, trend or [])
    _narrative(story, st, narrative)
    _limitations(report, st, story)

    buffer = io.BytesIO()
    footer = f"La Cuisine · {title} · {generated} (America/Bogota)"

    def canvas_maker(*args, **kwargs):
        return _NumberedCanvas(*args, footer_text=footer, **kwargs)

    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                                 topMargin=1.8 * cm, bottomMargin=2.0 * cm,
                                 title=title, author="La Cuisine", subject=period)
    document.build(story, canvasmaker=canvas_maker)
    data = buffer.getvalue()
    if len(data) > MAX_PDF_BYTES:
        raise PdfTooLargeError()
    return data
