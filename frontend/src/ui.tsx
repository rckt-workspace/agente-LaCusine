import { useCallback, useEffect, useState, type ReactNode } from "react";
import { messageFor } from "./api";

export function useFetch<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    load()
      .then((result) => active && setData(result))
      .catch((err: unknown) => active && setError(messageFor(err)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return { data, error, loading, reload };
}

export function Card({ title, children, action }: { title?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
      {(title || action) && (
        <header className="flex items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
          {title && <h2 className="text-sm font-semibold text-slate-800">{title}</h2>}
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Kpi({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function Loading() {
  return <p className="py-8 text-center text-sm text-slate-500">Cargando datos…</p>;
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <span>{message}</span>
      {onRetry && (
        <button onClick={onRetry} className="rounded-md bg-red-700 px-3 py-1.5 text-white hover:bg-red-800">
          Reintentar
        </button>
      )}
    </div>
  );
}

export function Empty({ text }: { text: string }) {
  return <p className="rounded-lg border border-dashed border-slate-300 py-8 text-center text-sm text-slate-500">{text}</p>;
}

const BADGE_TONES: Record<string, string> = {
  automated: "bg-emerald-100 text-emerald-800",
  paused_for_human: "bg-amber-100 text-amber-800",
  disabled: "bg-slate-200 text-slate-700",
  open: "bg-amber-100 text-amber-800",
  assigned: "bg-sky-100 text-sky-800",
  resolved: "bg-emerald-100 text-emerald-800",
  cancelled: "bg-slate-200 text-slate-700",
  won: "bg-emerald-100 text-emerald-800",
  lost: "bg-red-100 text-red-800",
};

export const LABELS: Record<string, string> = {
  automated: "IA activa",
  paused_for_human: "Atiende asesor",
  disabled: "IA deshabilitada",
  active: "Activa",
  waiting_customer: "Esperando cliente",
  closed: "Cerrada",
  open: "Abierta",
  assigned: "Asignada",
  resolved: "Resuelta",
  cancelled: "Cancelada",
  new: "Nuevo",
  qualifying: "Calificando",
  qualified: "Calificado",
  quoted: "Cotizado",
  handed_off: "Transferido",
  won: "Ganado",
  lost: "Perdido",
  opted_out: "No contactar",
  purchase_intent: "Intención de compra",
  quote_request: "Solicitud de cotización",
  installation: "Instalación",
  warranty: "Garantía",
  complaint: "Queja",
  unsupported_request: "Solicitud no soportada",
  customer_request: "Pidió asesor",
  other: "Otro",
  inbound: "Cliente",
  outbound: "Agente",
};

export function Badge({ value }: { value: string }) {
  const tone = BADGE_TONES[value] ?? "bg-slate-100 text-slate-700";
  return <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}>{LABELS[value] ?? value}</span>;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("es-CO", { dateStyle: "short", timeStyle: "short", timeZone: "America/Bogota" });
}

export function formatCop(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return Number(value).toLocaleString("es-CO", { style: "currency", currency: "COP", maximumFractionDigits: 0 });
}

export function formatUsd(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("es-CO", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

export function Pager({ offset, limit, total, onChange }: { offset: number; limit: number; total: number; onChange: (o: number) => void }) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <div className="mt-3 flex items-center justify-between text-sm text-slate-600">
      <span>{total === 0 ? "Sin resultados" : `${from}–${to} de ${total}`}</span>
      <div className="flex gap-2">
        <button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}
          className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-40">Anterior</button>
        <button disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}
          className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-40">Siguiente</button>
      </div>
    </div>
  );
}

export const inputClass =
  "w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-200";
export const buttonClass =
  "rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50";
