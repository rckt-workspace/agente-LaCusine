import { useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, Empty, ErrorBox, Kpi, LABELS, Loading, useFetch } from "../ui";

type Analytics = {
  days: number;
  messages_by_day: { day: string; inbound: number; outbound: number }[];
  conversations_started: number;
  handoffs_by_reason: { reason: string; count: number }[];
  leads_by_source: { source: string; count: number }[];
};

export function Analytics() {
  const { token } = useAuth();
  const [days, setDays] = useState(7);
  const { data, error, loading, reload } = useFetch<Analytics>(
    () => api.get<Analytics>(`/analytics?days=${days}`, token as string), [token, days]);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Analítica</h1>
          <p className="text-sm text-slate-500">Calculada desde los mensajes, conversaciones y leads guardados.</p>
        </div>
        <div className="flex gap-2">
          {[7, 30, 90].map((d) => (
            <button key={d} onClick={() => setDays(d)} aria-pressed={days === d}
              className={`rounded-md px-3 py-1.5 text-sm ${days === d ? "bg-indigo-600 text-white" : "border border-slate-300 bg-white"}`}>
              {d} días
            </button>
          ))}
        </div>
      </header>

      {loading ? <Loading /> : error || !data ? (
        <ErrorBox message={error ?? "Sin datos"} onRetry={reload} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
            <Kpi label="Conversaciones iniciadas" value={data.conversations_started} />
            <Kpi label="Solicitudes de asesor" value={data.handoffs_by_reason.reduce((s, h) => s + h.count, 0)} />
            <Kpi label="Días con actividad" value={data.messages_by_day.length} />
          </div>

          <Card title="Mensajes por día">
            {data.messages_by_day.length === 0 ? (
              <Empty text="No hay mensajes en este periodo." />
            ) : (
              <MessageBars rows={data.messages_by_day} />
            )}
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Solicitudes de asesor por motivo">
              {data.handoffs_by_reason.length === 0 ? <Empty text="Sin solicitudes en este periodo." /> : (
                <ul className="divide-y divide-slate-100 text-sm">
                  {data.handoffs_by_reason.map((h) => (
                    <li key={h.reason} className="flex justify-between py-2"><span>{LABELS[h.reason] ?? h.reason}</span><span className="font-semibold">{h.count}</span></li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Leads por origen">
              {data.leads_by_source.length === 0 ? <Empty text="Sin leads en este periodo." /> : (
                <ul className="divide-y divide-slate-100 text-sm">
                  {data.leads_by_source.map((s) => (
                    <li key={s.source} className="flex justify-between py-2"><span>{s.source}</span><span className="font-semibold">{s.count}</span></li>
                  ))}
                </ul>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function MessageBars({ rows }: { rows: { day: string; inbound: number; outbound: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => Math.max(r.inbound, r.outbound)));
  return (
    <div className="space-y-3">
      <div className="flex gap-4 text-xs text-slate-600">
        <span className="flex items-center gap-1"><i className="inline-block h-2 w-3 rounded bg-slate-400" /> Clientes</span>
        <span className="flex items-center gap-1"><i className="inline-block h-2 w-3 rounded bg-indigo-500" /> Agente</span>
      </div>
      <div className="overflow-x-auto">
        <div className="flex min-w-full items-end gap-2" style={{ minHeight: 160 }}>
          {rows.map((r) => (
            <div key={r.day} className="flex flex-1 flex-col items-center gap-1" title={`${r.day}: ${r.inbound} clientes, ${r.outbound} agente`}>
              <div className="flex h-36 items-end gap-1">
                <div className="w-3 rounded-t bg-slate-400" style={{ height: `${(r.inbound / max) * 100}%` }} />
                <div className="w-3 rounded-t bg-indigo-500" style={{ height: `${(r.outbound / max) * 100}%` }} />
              </div>
              <span className="text-[10px] text-slate-500">{r.day.slice(5)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
