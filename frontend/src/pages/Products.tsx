import { useState, type FormEvent } from "react";
import { api, type Page } from "../api";
import { useAuth } from "../auth";
import { Card, Empty, ErrorBox, formatCop, formatDate, inputClass, Loading, Pager, useFetch } from "../ui";

type Product = {
  id: string; reference: string; brand: string; category: string; price_cop: string | null;
  availability_label: string | null; availability_as_of: string | null; product_url: string | null; updated_at: string;
};

const LIMIT = 25;

export function Products() {
  const { token } = useAuth();
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);

  const list = useFetch<Page<Product>>(() => {
    const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (search) params.set("q", search);
    return api.get<Page<Product>>(`/products?${params}`, token as string);
  }, [token, search, offset]);

  function submit(event: FormEvent) {
    event.preventDefault();
    setOffset(0);
    setSearch(q.trim());
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900">Catálogo</h1>
        <p className="text-sm text-slate-500">Precios de referencia y disponibilidad según la última importación.</p>
      </header>
      <form onSubmit={submit} className="flex gap-2">
        <input className={inputClass} placeholder="Buscar por referencia, marca o categoría" value={q} maxLength={80}
          onChange={(e) => setQ(e.target.value)} aria-label="Buscar productos" />
        <button type="submit" className="rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700">Buscar</button>
      </form>
      <Card>
        {list.loading ? <Loading /> : list.error || !list.data ? (
          <ErrorBox message={list.error ?? "Sin datos"} onRetry={list.reload} />
        ) : list.data.items.length === 0 ? (
          <Empty text="No hay productos con este criterio." />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="py-2 pr-4">Referencia</th><th className="py-2 pr-4">Marca</th>
                    <th className="py-2 pr-4">Categoría</th><th className="py-2 pr-4">Precio ref.</th>
                    <th className="py-2 pr-4">Disponibilidad</th><th className="py-2">Actualizado</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {list.data.items.map((p) => (
                    <tr key={p.id}>
                      <td className="py-2 pr-4 font-medium text-slate-800">
                        {p.product_url ? <a className="text-indigo-700 hover:underline" href={p.product_url} target="_blank" rel="noreferrer">{p.reference}</a> : p.reference}
                      </td>
                      <td className="py-2 pr-4">{p.brand}</td>
                      <td className="py-2 pr-4">{p.category}</td>
                      <td className="py-2 pr-4">{formatCop(p.price_cop)}</td>
                      <td className="py-2 pr-4">{p.availability_label ?? "Sin dato"}{p.availability_as_of ? ` (${p.availability_as_of})` : ""}</td>
                      <td className="py-2">{formatDate(p.updated_at)}</td>
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
