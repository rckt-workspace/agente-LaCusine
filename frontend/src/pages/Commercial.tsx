import { useState, type FormEvent } from "react";
import { api, ApiError, messageFor } from "../api";
import { useAuth } from "../auth";
import { Card, Empty, ErrorBox, Kpi, Loading, useFetch } from "../ui";
import { cop, pct, usd } from "../format";

// Tipos alineados con la respuesta de /api/admin/commercial/*. Las cifras las calcula el backend desde PostgreSQL.
type Report = {
  semana: { inicio_bogota: string; fin_exclusivo_bogota: string; zona_horaria: string };
  actividad: {
    mensajes_entrantes: number; respuestas_aceptadas_por_meta: number; respuestas_entregadas_o_leidas: number;
    respuestas_fallidas: number; tasa_entrega_pct: number | null; conversaciones_nuevas: number;
    conversaciones_derivadas_a_asesor: number; consultas_fallidas: number;
  };
  prospectos: {
    leads_nuevos: number; leads_calificados: number; leads_transferidos_a_asesor: number;
    leads_con_intencion_de_compra: number | null; leads_con_cotizacion_solicitada: number | null;
    productos_mas_consultados: { marca: string; categoria: string; consultas: number }[] | null;
    origen_prospectos: { estado: string; mensaje?: string; detalle: Record<string, number> };
  };
  ventas: {
    estado: "disponible" | "sin_datos"; motivo: string; ventas_confirmadas: number | null;
    valor_confirmado_cop: number | null; ticket_promedio_cop: number | null; ventas_canceladas: number | null;
    conversion_lead_a_venta_pct: number | null;
  };
  operacion_ia: {
    llamadas_exitosas: number; llamadas_fallidas: number; costo_estimado_usd: number | null;
    costo_por_conversacion_usd: number | null; costo_por_lead_atendido_usd: number | null;
  };
  comparacion_semana_anterior?: Record<string, number | null>;
  resumen_ejecutivo: string[];
};
type Trend = { semanas: { semana_inicio: string; mensajes_entrantes: number; conversaciones_nuevas: number;
  ventas_confirmadas: number; ingresos_cop: number }[] };
type Sale = { sale_ref: string; sold_at: string; amount_cop: number; source: string; confirmed_by: string; status: string };

const SOURCE_LABELS: Record<string, string> = {
  admin_manual: "Confirmada manualmente", external_order_system: "Sistema de pedidos",
};

function todayIso(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "America/Bogota" });
}

const REPORT_KINDS: { id: string; label: string }[] = [
  { id: "operativo", label: "Operativo semanal" },
  { id: "marketing", label: "Marketing y captación" },
  { id: "oportunidades", label: "Oportunidades" },
  { id: "ventas", label: "Ventas confirmadas" },
  { id: "ejecutivo", label: "Ejecutivo consolidado" },
];

export function Commercial({ role }: { role: string }) {
  const { token } = useAuth();
  const [day, setDay] = useState(todayIso());
  const [kind, setKind] = useState("operativo");
  const [narrative, setNarrative] = useState(false);
  const [busy, setBusy] = useState(false);
  const report = useFetch<Report>(
    () => api.get<Report>(`/commercial/report?day=${day}`, token as string), [token, day]);
  const trend = useFetch<Trend>(() => api.get<Trend>("/commercial/trend?weeks=6", token as string), [token]);
  const sales = useFetch<{ items: Sale[] }>(() => api.get<{ items: Sale[] }>("/commercial/sales?limit=50", token as string), [token]);
  const [notice, setNotice] = useState<string | null>(null);

  async function download(path: string, filename: string) {
    setNotice(null);
    setBusy(true);
    try {
      const response = await fetch(`/api/admin${path}`, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) {
        let code = "request_failed";
        try {
          const data = (await response.json()) as { detail?: unknown };
          if (typeof data.detail === "string") code = data.detail;
        } catch {
          // Sin cuerpo JSON: se mantiene el código genérico.
        }
        throw new ApiError(response.status, code);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setNotice(messageFor(error));
    } finally {
      setBusy(false);
    }
  }

  function downloadCsv() {
    return download(`/commercial/report.csv?day=${day}`, `informe-semanal-${day}.csv`);
  }

  function downloadPdf() {
    const withNarrative = narrative && kind === "ejecutivo";
    return download(`/commercial/report.pdf?kind=${kind}&day=${day}&narrative=${withNarrative}`,
      `la-cuisine-${kind}-${day}.pdf`);
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Inteligencia comercial</h1>
          <p className="text-sm text-slate-500">
            Semana de lunes a lunes, hora de Bogotá. Las cifras salen de la base de datos; sin datos no se estima nada.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <label className="text-sm text-slate-600">
            Día de la semana{" "}
            <input type="date" value={day} onChange={(e) => setDay(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1" />
          </label>
          <label className="text-sm text-slate-600">
            Informe{" "}
            <select value={kind} onChange={(e) => setKind(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1">
              {REPORT_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
            </select>
          </label>
          {kind === "ejecutivo" && (
            <label className="flex items-center gap-1 text-sm text-slate-600">
              <input type="checkbox" checked={narrative} onChange={(e) => setNarrative(e.target.checked)} />
              Incluir análisis con IA
            </label>
          )}
          <button onClick={downloadPdf} disabled={busy}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">
            {busy ? "Generando…" : "Generar PDF"}
          </button>
          <button onClick={downloadCsv} disabled={busy}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm disabled:opacity-50">
            Descargar CSV
          </button>
        </div>
      </header>
      {notice && <ErrorBox message={notice} />}

      {report.loading ? <Loading /> : report.error || !report.data ? (
        <ErrorBox message={report.error ?? "Sin datos"} onRetry={report.reload} />
      ) : (
        <ReportView report={report.data} />
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Evolución de las últimas semanas">
          {trend.loading ? <Loading /> : trend.error || !trend.data ? (
            <ErrorBox message={trend.error ?? "Sin datos"} onRetry={trend.reload} />
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-slate-500">
                <tr><th>Inicio</th><th>Mensajes</th><th>Conversaciones</th><th>Ventas</th><th>Ingresos</th></tr>
              </thead>
              <tbody>
                {trend.data.semanas.map((w) => (
                  <tr key={w.semana_inicio} className="border-t border-slate-100">
                    <td className="py-1">{w.semana_inicio}</td><td>{w.mensajes_entrantes}</td>
                    <td>{w.conversaciones_nuevas}</td><td>{w.ventas_confirmadas}</td><td>{cop(w.ingresos_cop)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <SalesCard role={role} sales={sales} onChanged={sales.reload} />
      </div>
    </div>
  );
}

function ReportView({ report }: { report: Report }) {
  const sales = report.ventas;
  const origin = report.prospectos.origen_prospectos;
  return (
    <>
      <Card title={`Resumen ejecutivo · ${report.semana.inicio_bogota} a ${report.semana.fin_exclusivo_bogota}`}>
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
          {report.resumen_ejecutivo.map((line) => <li key={line}>{line}</li>)}
        </ul>
      </Card>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi label="Mensajes recibidos" value={report.actividad.mensajes_entrantes} />
        <Kpi label="Respuestas aceptadas por Meta" value={report.actividad.respuestas_aceptadas_por_meta} />
        <Kpi label="Entregadas o leídas" value={report.actividad.respuestas_entregadas_o_leidas}
          hint={pct(report.actividad.tasa_entrega_pct)} />
        <Kpi label="Leads nuevos" value={report.prospectos.leads_nuevos} />
        <Kpi label="Leads calificados" value={report.prospectos.leads_calificados} />
        <Kpi label="Intención de compra" value={report.prospectos.leads_con_intencion_de_compra ?? "Sin datos"}
          hint="No es una venta" />
        <Kpi label="Cotizaciones solicitadas" value={report.prospectos.leads_con_cotizacion_solicitada ?? "Sin datos"} />
        <Kpi label="Ventas confirmadas" value={sales.ventas_confirmadas ?? "Sin datos"} hint={sales.motivo} />
        <Kpi label="Ingresos confirmados" value={cop(sales.valor_confirmado_cop)} />
        <Kpi label="Conversión a venta" value={pct(sales.conversion_lead_a_venta_pct)} hint="Cohorte de leads de la semana" />
        <Kpi label="Costo estimado de IA" value={usd(report.operacion_ia.costo_estimado_usd)} />
        <Kpi label="Consultas fallidas" value={report.actividad.consultas_fallidas} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Productos y marcas más consultados">
          {report.prospectos.productos_mas_consultados === null ? <Empty text="Sin datos de interés por producto." /> :
            report.prospectos.productos_mas_consultados.length === 0 ? <Empty text="Sin consultas por producto esta semana." /> : (
              <ul className="divide-y divide-slate-100 text-sm">
                {report.prospectos.productos_mas_consultados.map((p) => (
                  <li key={`${p.marca}-${p.categoria}`} className="flex justify-between py-2">
                    <span>{p.marca} · {p.categoria}</span><span className="font-semibold">{p.consultas}</span>
                  </li>
                ))}
              </ul>
            )}
        </Card>
        <Card title="Origen de los prospectos">
          {origin.estado === "sin_informacion" ? <Empty text={origin.mensaje ?? "Sin información de origen disponible."} /> :
            origin.estado === "sin_leads" ? <Empty text="No hay leads nuevos esta semana." /> : (
              <ul className="divide-y divide-slate-100 text-sm">
                {Object.entries(origin.detalle).map(([source, count]) => (
                  <li key={source} className="flex justify-between py-2"><span>{source}</span><span className="font-semibold">{count}</span></li>
                ))}
              </ul>
            )}
        </Card>
      </div>

      <Card title="Rendimiento del agente">
        <div className="grid grid-cols-2 gap-4 text-sm lg:grid-cols-4">
          <div><span className="text-slate-500">Llamadas exitosas</span><div className="font-semibold">{report.operacion_ia.llamadas_exitosas}</div></div>
          <div><span className="text-slate-500">Llamadas fallidas</span><div className="font-semibold">{report.operacion_ia.llamadas_fallidas}</div></div>
          <div><span className="text-slate-500">Costo por conversación</span><div className="font-semibold">{usd(report.operacion_ia.costo_por_conversacion_usd)}</div></div>
          <div><span className="text-slate-500">Costo por lead atendido</span><div className="font-semibold">{usd(report.operacion_ia.costo_por_lead_atendido_usd)}</div></div>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Los bloqueos por presupuesto no se guardan todavía; solo quedan en el registro del servidor.
        </p>
      </Card>
    </>
  );
}

function SalesCard({ role, sales, onChanged }: {
  role: string;
  sales: { data: { items: Sale[] } | null; error: string | null; loading: boolean; reload: () => void };
  onChanged: () => void;
}) {
  const { token } = useAuth();
  const isAdmin = role === "admin";
  const [form, setForm] = useState({ sale_ref: "", lead_id: "", amount_cop: "", sold_at: "", evidence_ref: "",
    source: "admin_manual" });
  const [message, setMessage] = useState<string | null>(null);

  async function confirm(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    try {
      await api.post("/commercial/sales", token as string, {
        sale_ref: form.sale_ref.trim(), lead_id: form.lead_id.trim(), amount_cop: Number(form.amount_cop),
        sold_at: new Date(form.sold_at).toISOString(), source: form.source,
        evidence_ref: form.evidence_ref.trim(), products: {},
      });
      setMessage("Venta registrada.");
      setForm({ ...form, sale_ref: "", amount_cop: "", evidence_ref: "" });
      onChanged();
    } catch (error) {
      setMessage(messageFor(error));
    }
  }

  async function cancel(saleRef: string) {
    const reason = window.prompt("Motivo de la cancelación (queda registrado):");
    if (!reason || reason.trim().length < 5) return;
    try {
      await api.post(`/commercial/sales/${encodeURIComponent(saleRef)}/cancel`, token as string, { reason: reason.trim() });
      onChanged();
    } catch (error) {
      setMessage(messageFor(error));
    }
  }

  return (
    <Card title="Ventas confirmadas">
      <p className="mb-2 text-xs text-slate-500">
        Una intención de compra no es una venta. Solo cuentan las ventas confirmadas aquí, con evidencia.
      </p>
      {sales.loading ? <Loading /> : sales.error || !sales.data ? (
        <ErrorBox message={sales.error ?? "Sin datos"} onRetry={sales.reload} />
      ) : sales.data.items.length === 0 ? <Empty text="Todavía no hay ventas registradas." /> : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-500"><tr><th>Referencia</th><th>Fecha</th><th>Importe</th><th>Origen</th><th>Estado</th><th /></tr></thead>
          <tbody>
            {sales.data.items.map((s) => (
              <tr key={s.sale_ref} className="border-t border-slate-100">
                <td className="py-1">{s.sale_ref}</td>
                <td>{s.sold_at.slice(0, 10)}</td>
                <td>{cop(s.amount_cop)}</td>
                <td>{SOURCE_LABELS[s.source] ?? s.source}</td>
                <td>{s.status === "confirmed" ? "Confirmada" : "Cancelada"}</td>
                <td>
                  {isAdmin && s.status === "confirmed" && (
                    <button onClick={() => cancel(s.sale_ref)} className="text-xs text-rose-700 underline">Cancelar</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {isAdmin ? (
        <form onSubmit={confirm} className="mt-4 grid gap-2 border-t border-slate-100 pt-4 text-sm">
          <h3 className="font-semibold text-slate-800">Confirmar venta</h3>
          <input required placeholder="Referencia de la venta" value={form.sale_ref}
            onChange={(e) => setForm({ ...form, sale_ref: e.target.value })} className="rounded-md border px-2 py-1" />
          <input required placeholder="Identificador interno del lead (UUID)" value={form.lead_id}
            onChange={(e) => setForm({ ...form, lead_id: e.target.value })} className="rounded-md border px-2 py-1" />
          <input required type="number" min={1} placeholder="Importe en COP" value={form.amount_cop}
            onChange={(e) => setForm({ ...form, amount_cop: e.target.value })} className="rounded-md border px-2 py-1" />
          <input required type="datetime-local" value={form.sold_at}
            onChange={(e) => setForm({ ...form, sold_at: e.target.value })} className="rounded-md border px-2 py-1" />
          <select value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })}
            className="rounded-md border px-2 py-1">
            <option value="admin_manual">Confirmada manualmente</option>
            <option value="external_order_system">Sistema de pedidos</option>
          </select>
          <input required minLength={3} placeholder="Evidencia (factura, pedido, comprobante)" value={form.evidence_ref}
            onChange={(e) => setForm({ ...form, evidence_ref: e.target.value })} className="rounded-md border px-2 py-1" />
          <button type="submit" className="rounded-md bg-indigo-600 px-3 py-1.5 text-white">Registrar venta</button>
          {message && <p className="text-xs text-slate-600">{message}</p>}
        </form>
      ) : (
        <p className="mt-4 text-xs text-slate-500">Solo un administrador puede confirmar o cancelar ventas.</p>
      )}
    </Card>
  );
}
