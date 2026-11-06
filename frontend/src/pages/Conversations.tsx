import { useState, type FormEvent } from "react";
import { api, messageFor, type Page } from "../api";
import { useAuth } from "../auth";
import { Badge, buttonClass, Card, Empty, ErrorBox, formatDate, inputClass, LABELS, Loading, Pager, useFetch } from "../ui";

type ConversationRow = {
  id: string; status: string; automation_state: string; last_activity_at: string; contact: string | null;
  display_name: string | null; lead_status: string; product_interest: string | null; open_handoffs: number;
};
type ConversationDetail = {
  id: string; status: string; automation_state: string; last_activity_at: string;
  lead: { contact: string | null; display_name: string | null; status: string; city: string | null;
    product_interest: string | null; budget_cop: number | null };
  messages: { direction: string; message_type: string; processing_status: string; content_text: string | null; received_at: string }[];
  handoffs: { id: string; reason: string; status: string; priority: string; assigned_advisor: string | null; opened_at: string }[];
};

const LIMIT = 25;
const STATES = ["", "automated", "paused_for_human", "disabled"];

export function Conversations({ role }: { role: string }) {
  const { token } = useAuth();
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  const list = useFetch<Page<ConversationRow>>(() => {
    const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
    if (state) params.set("state", state);
    if (search) params.set("q", search);
    return api.get<Page<ConversationRow>>(`/conversations?${params}`, token as string);
  }, [token, state, search, offset]);

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setOffset(0);
    setSearch(q.trim());
  }

  return (
    <div className="grid gap-6 xl:grid-cols-5">
      <div className="space-y-4 xl:col-span-2">
        <header>
          <h1 className="text-2xl font-semibold text-slate-900">Conversaciones</h1>
          <p className="text-sm text-slate-500">Los números se muestran enmascarados.</p>
        </header>
        <form onSubmit={submitSearch} className="flex flex-col gap-2 sm:flex-row">
          <input className={inputClass} placeholder="Buscar por nombre o últimos dígitos" value={q}
            onChange={(e) => setQ(e.target.value)} maxLength={80} aria-label="Buscar conversaciones" />
          <select className={`${inputClass} sm:w-56`} value={state} aria-label="Filtrar por estado"
            onChange={(e) => { setState(e.target.value); setOffset(0); }}>
            {STATES.map((s) => <option key={s} value={s}>{s ? LABELS[s] : "Todos los estados"}</option>)}
          </select>
          <button className={buttonClass} type="submit">Buscar</button>
        </form>

        <Card>
          {list.loading ? <Loading /> : list.error || !list.data ? (
            <ErrorBox message={list.error ?? "Sin datos"} onRetry={list.reload} />
          ) : list.data.items.length === 0 ? (
            <Empty text="No hay conversaciones con estos filtros." />
          ) : (
            <>
              <ul className="divide-y divide-slate-100">
                {list.data.items.map((c) => (
                  <li key={c.id}>
                    <button onClick={() => setSelected(c.id)} aria-current={selected === c.id}
                      className={`flex w-full flex-col gap-1 rounded-md px-2 py-3 text-left hover:bg-slate-50 ${selected === c.id ? "bg-indigo-50" : ""}`}>
                      <span className="flex items-center justify-between gap-2">
                        <span className="font-medium text-slate-800">{c.display_name ?? "Sin nombre"}</span>
                        <Badge value={c.automation_state} />
                      </span>
                      <span className="flex items-center justify-between text-xs text-slate-500">
                        <span>{c.contact ?? "—"} · {c.product_interest ?? "sin producto indicado"}</span>
                        <span>{formatDate(c.last_activity_at)}</span>
                      </span>
                      {c.open_handoffs > 0 && (
                        <span className="text-xs font-medium text-amber-700">{c.open_handoffs} solicitud(es) de asesor</span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
              <Pager offset={offset} limit={LIMIT} total={list.data.total} onChange={setOffset} />
            </>
          )}
        </Card>
      </div>

      <div className="xl:col-span-3">
        {selected ? (
          <ConversationPanel key={selected} conversationId={selected} role={role} onChanged={list.reload} />
        ) : (
          <Empty text="Selecciona una conversación para ver los mensajes y las acciones disponibles." />
        )}
      </div>
    </div>
  );
}

function ConversationPanel({ conversationId, role, onChanged }: { conversationId: string; role: string; onChanged: () => void }) {
  const { token } = useAuth();
  const detail = useFetch<ConversationDetail>(
    () => api.get<ConversationDetail>(`/conversations/${conversationId}`, token as string), [token, conversationId]);
  const [reason, setReason] = useState("");
  const [advisor, setAdvisor] = useState("");
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(path: string, body: Record<string, string>, okText: string) {
    setBusy(true);
    setFeedback(null);
    try {
      await api.post(path, token as string, body);
      setFeedback({ ok: true, text: okText });
      detail.reload();
      onChanged();
    } catch (err) {
      setFeedback({ ok: false, text: messageFor(err) });
    } finally {
      setBusy(false);
    }
  }

  if (detail.loading) return <Loading />;
  if (detail.error || !detail.data) return <ErrorBox message={detail.error ?? "Sin datos"} onRetry={detail.reload} />;
  const c = detail.data;
  const openHandoff = c.handoffs.find((h) => h.status === "open" || h.status === "assigned");
  const canSubmit = reason.trim().length >= 3 && !busy;

  return (
    <div className="space-y-4">
      <Card title={c.lead.display_name ?? "Cliente sin nombre"} action={<Badge value={c.automation_state} />}>
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
          <div><dt className="text-slate-500">Contacto</dt><dd>{c.lead.contact ?? "—"}</dd></div>
          <div><dt className="text-slate-500">Estado del lead</dt><dd><Badge value={c.lead.status} /></dd></div>
          <div><dt className="text-slate-500">Ciudad</dt><dd>{c.lead.city ?? "—"}</dd></div>
          <div><dt className="text-slate-500">Producto de interés</dt><dd>{c.lead.product_interest ?? "—"}</dd></div>
          <div><dt className="text-slate-500">Última actividad</dt><dd>{formatDate(c.last_activity_at)}</dd></div>
        </dl>
      </Card>

      <Card title="Acciones">
        <div className="space-y-3">
          <label className="block text-sm font-medium text-slate-700">
            Motivo (obligatorio, queda en auditoría)
            <input className={`${inputClass} mt-1`} value={reason} maxLength={500}
              onChange={(e) => setReason(e.target.value)} placeholder="Ej.: el cliente pidió cotización" />
          </label>
          <div className="flex flex-wrap gap-2">
            {c.automation_state === "automated" && (
              <button className={buttonClass} disabled={!canSubmit}
                onClick={() => void run(`/conversations/${c.id}/pause`, { reason: reason.trim() },
                  "La IA quedó pausada para el asesor.")}>
                Pausar IA y atender
              </button>
            )}
            {c.automation_state !== "automated" && role === "admin" && (
              <button className={buttonClass} disabled={!canSubmit}
                onClick={() => void run(`/conversations/${c.id}/resume`, { reason: reason.trim() },
                  "La IA volvió a responder.")}>
                Reanudar IA
              </button>
            )}
            {openHandoff?.status === "open" && (
              <div className="flex flex-1 flex-col gap-2 sm:flex-row">
                <input className={inputClass} placeholder="Nombre del asesor" value={advisor} maxLength={120}
                  onChange={(e) => setAdvisor(e.target.value)} aria-label="Nombre del asesor" />
                <button className={buttonClass} disabled={!canSubmit || !advisor.trim()}
                  onClick={() => void run(`/handoffs/${openHandoff.id}/assign`,
                    { reason: reason.trim(), advisor: advisor.trim() }, "Solicitud asignada.")}>
                  Asignar
                </button>
              </div>
            )}
            {openHandoff && (
              <button className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
                disabled={!canSubmit}
                onClick={() => void run(`/handoffs/${openHandoff.id}/resolve`, { reason: reason.trim() },
                  "Solicitud resuelta.")}>
                Marcar resuelta
              </button>
            )}
          </div>
          {feedback && (
            <p role="status" className={`text-sm ${feedback.ok ? "text-emerald-700" : "text-red-700"}`}>{feedback.text}</p>
          )}
        </div>
      </Card>

      <Card title="Mensajes">
        {c.messages.length === 0 ? (
          <Empty text="Esta conversación todavía no tiene mensajes." />
        ) : (
          <ol className="max-h-[480px] space-y-3 overflow-y-auto pr-1">
            {c.messages.map((m, i) => (
              <li key={i} className={`flex ${m.direction === "outbound" ? "justify-end" : "justify-start"}`}>
                <div className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
                  m.direction === "outbound" ? "bg-indigo-50 text-indigo-950" : "bg-slate-100 text-slate-800"}`}>
                  <p className="whitespace-pre-wrap break-words">
                    {m.content_text ?? (m.message_type === "text" ? "(contenido expirado)" : `[${m.message_type}]`)}
                  </p>
                  <p className="mt-1 text-[11px] text-slate-500">
                    <Badge value={m.direction} /> · {formatDate(m.received_at)}
                    {m.processing_status === "failed" && " · error de procesamiento"}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>

      <Card title="Solicitudes de asesor">
        {c.handoffs.length === 0 ? (
          <Empty text="No hay solicitudes de asesor para esta conversación." />
        ) : (
          <ul className="divide-y divide-slate-100 text-sm">
            {c.handoffs.map((h) => (
              <li key={h.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <span>{LABELS[h.reason] ?? h.reason} · {formatDate(h.opened_at)}</span>
                <span className="flex items-center gap-2">
                  {h.assigned_advisor && <span className="text-slate-600">{h.assigned_advisor}</span>}
                  <Badge value={h.status} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
