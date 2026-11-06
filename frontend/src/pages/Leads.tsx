import { useState } from "react";
import { api, type Page } from "../api";
import { useAuth } from "../auth";
import { Badge, Card, Empty, ErrorBox, formatCop, formatDate, inputClass, LABELS, Loading, Pager, useFetch } from "../ui";

type Lead = {
  id: string; contact: string | null; display_name: string | null; city: string | null; product_interest: string | null;
  budget_cop: number | null; status: string; acquisition_source: string | null; created_at: string; updated_at: string;
};

const LIMIT = 25;
const STATUSES = ["new", "qualifying", "qualified", "quoted", "handed_off", "won", "lost", "opted_out"];

export function Leads() {
  const { token } = useAuth();
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);

  const list = useFetch<Page<Lead>>(() => {
    const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (status) params.set("status", status);
    if (search) params.set("q", search);
    return api.get<Page<Lead>>(`/leads?${params}`, token as string);
  }, [token, status, search, offset]);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900">Leads</h1>
        <p className="text-sm text-slate-500">
          Consulta de leads. La edición de estado todavía no está disponible: requiere ampliar la auditoría a leads.
        </p>
      </header>
      <form className="flex flex-col gap-2 sm:flex-row" onSubmit={(e) => { e.preventDefault(); setOffset(0); setSearch(q.trim()); }}>
        <input className={inputClass} placeholder="Buscar por nombre o últimos dígitos" value={q} maxLength={80}
          onChange={(e) => setQ(e.target.value)} aria-label="Buscar leads" />
        <select className={`${inputClass} sm:w-56`} value={status} aria-label="Filtrar por estado"
          onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
          <option value="">Todos los estados</option>
          {STATUSES.map((s) => <option key={s} value={s}>{LABELS[s]}</option>)}
        </select>
      </form>

      <Card>
        {list.loading ? <Loading /> : list.error || !list.data ? (
          <ErrorBox message={list.error ?? "Sin datos"} onRetry={list.reload} />
        ) : list.data.items.length === 0 ? (
          <Empty text="No hay leads con estos filtros." />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="py-2 pr-4">Nombre</th><th className="py-2 pr-4">Contacto</th>
                    <th className="py-2 pr-4">Producto</th><th className="py-2 pr-4">Presupuesto</th>
                    <th className="py-2 pr-4">Estado</th><th className="py-2 pr-4">Origen</th>
                    <th className="py-2">Actualizado</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {list.data.items.map((l) => (
                    <tr key={l.id}>
                      <td className="py-2 pr-4 font-medium text-slate-800">{l.display_name ?? "Sin nombre"}</td>
                      <td className="py-2 pr-4">{l.contact ?? "—"}</td>
                      <td className="py-2 pr-4">{l.product_interest ?? "—"}</td>
                      <td className="py-2 pr-4">{formatCop(l.budget_cop)}</td>
                      <td className="py-2 pr-4"><Badge value={l.status} /></td>
                      <td className="py-2 pr-4">{l.acquisition_source ?? "—"}</td>
                      <td className="py-2">{formatDate(l.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pager offset={offset} limit={LIMIT} total={list.data.total} onChange={setOffset} />
          </>
        )}
      </Card>
    </div>
  );
}
