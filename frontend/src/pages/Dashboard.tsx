import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, Card, ErrorBox, Kpi, Loading, formatDate, formatUsd, useFetch } from "../ui";
import type { Route } from "../Layout";

export type DashboardData = {
  conversations: { automated: number; paused_for_human: number; disabled: number };
  open_handoffs: number;
  messages_today: { inbound: number; outbound: number };
  leads_today: number;
  leads_by_status: Record<string, number>;
  llm: {
    cost_today_usd: string; cost_month_usd: string; failed_calls_today: number;
    daily_budget_usd: string | null; monthly_budget_usd: string | null;
  };
  recent_conversations: { id: string; automation_state: string; last_activity_at: string; contact: string | null;
    display_name: string | null; lead_status: string }[];
};

export function Dashboard({ onNavigate }: { onNavigate: (r: Route) => void }) {
  const { token } = useAuth();
  const { data, error, loading, reload } = useFetch<DashboardData>(
    () => api.get<DashboardData>("/dashboard", token as string), [token]);

  if (loading) return <Loading />;
  if (error || !data) return <ErrorBox message={error ?? "Sin datos"} onRetry={reload} />;

  const leadEntries = Object.entries(data.leads_by_status).sort((a, b) => b[1] - a[1]);
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900">Panel</h1>
        <p className="text-sm text-slate-500">Cifras calculadas desde la base de datos en hora de Bogotá.</p>
      </header>

      <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <Kpi label="Conversaciones con IA" value={data.conversations.automated} />
        <Kpi label="Atiende un asesor" value={data.conversations.paused_for_human} />
        <Kpi label="Solicitudes abiertas" value={data.open_handoffs} />
        <Kpi label="Leads nuevos hoy" value={data.leads_today} />
        <Kpi label="Mensajes de clientes hoy" value={data.messages_today.inbound} />
        <Kpi label="Respuestas enviadas hoy" value={data.messages_today.outbound} />
        <Kpi label="Costo IA hoy" value={formatUsd(data.llm.cost_today_usd)}
          hint={data.llm.daily_budget_usd ? `Presupuesto diario ${formatUsd(data.llm.daily_budget_usd)}` : "Sin presupuesto diario configurado"} />
        <Kpi label="Llamadas IA fallidas hoy" value={data.llm.failed_calls_today} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Leads por estado">
          {leadEntries.length === 0 ? (
            <p className="text-sm text-slate-500">Todavía no hay leads registrados.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {leadEntries.map(([status, count]) => (
                <li key={status} className="flex items-center justify-between py-2 text-sm">
                  <Badge value={status} /> <span className="font-semibold">{count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Actividad reciente" action={
          <button onClick={() => onNavigate("conversations")} className="text-sm text-indigo-600 hover:underline">Ver todas</button>}>
          {data.recent_conversations.length === 0 ? (
            <p className="text-sm text-slate-500">Todavía no hay conversaciones.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {data.recent_conversations.map((c) => (
                <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <span className="font-medium text-slate-800">{c.display_name ?? c.contact ?? "Sin nombre"}</span>
                  <span className="flex items-center gap-2"><Badge value={c.automation_state} />
                    <span className="text-xs text-slate-500">{formatDate(c.last_activity_at)}</span></span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <p className="text-xs text-slate-500">Costos estimados con las tarifas configuradas; no son facturas del proveedor.</p>
    </div>
  );
}
