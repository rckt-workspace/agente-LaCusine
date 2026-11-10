// Núcleo del conector: recepción de mensajes, decisión, reenvío al backend y envío de respuestas.
// Todas las dependencias (cliente WhatsApp, backend, reloj, estado de conexión) llegan por parámetro,
// así que el comportamiento completo se prueba sin WhatsApp real.
import { createHash } from "node:crypto";
import { forwardAllowed, sendAllowed, type WebjsConfig } from "./config.js";
import type { ForwardItem, ForwardResult } from "./forwarder.js";
import { classifyInbound, type InboundFacts, type PolicyState } from "./policy.js";
import { UnresolvedRouteError, type RouteTable } from "./routes.js";

export type ConnState =
  | "starting"
  | "qr_required"
  | "authenticated"
  | "ready"
  | "auth_failure"
  | "disconnected"
  | "logged_out";

// Contadores de solo lectura. Nunca contienen contenido ni identificadores.
export interface Diag {
  rawEventsTotal: number;
  ignoredTotal: number;
  stopBlockedTotal: number;
  messagesForwardedTotal: number;
  handlerErrorsTotal: number;
  sendsTotal: number;
  sendFailuresTotal: number;
  authFailuresTotal: number;
  disconnectsTotal: number;
  qrEventsTotal: number;
  lastRawAt: string | null;
  lastForwardAt: string | null;
}

export function newDiag(): Diag {
  return {
    rawEventsTotal: 0,
    ignoredTotal: 0,
    stopBlockedTotal: 0,
    messagesForwardedTotal: 0,
    handlerErrorsTotal: 0,
    sendsTotal: 0,
    sendFailuresTotal: 0,
    authFailuresTotal: 0,
    disconnectsTotal: 0,
    qrEventsTotal: 0,
    lastRawAt: null,
    lastForwardAt: null,
  };
}

export interface Transport {
  sendMessage(chatId: string, text: string): Promise<{ id?: { _serialized?: string } } | null | undefined>;
  // Comprobación previa: ¿WhatsApp resuelve el chat? Si no, nada puede salir. Opcional por compatibilidad.
  chatResolved?(chatId: string): Promise<boolean>;
  // Recuperación del identificador de un mensaje propio ya enviado: busca en el chat un mensaje saliente con el mismo
  // texto exacto enviado desde sinceSec. Devuelve su identificador o null. Opcional.
  recoverSentId?(chatId: string, text: string, sinceSec: number): Promise<string | null>;
}

// El chat no se resuelve en WhatsApp Web: no sale nada. Es un rechazo definitivo, no un envío incierto.
export class ChatNotResolvedError extends Error {
  constructor() {
    super("chat_not_resolved");
    this.name = "ChatNotResolvedError";
  }
}

export interface GatewayDeps {
  config: WebjsConfig;
  routes: RouteTable;
  policy: PolicyState;
  diag: Diag;
  state(): ConnState;
  isKilled(): boolean;
  nowSec(): number;
  transport: Transport;
  forward(items: ForwardItem[]): Promise<ForwardResult>;
  log(line: string): void;
  // Detiene el indicador «escribiendo…» del chat antes de enviar la respuesta (opcional).
  typingStop?(chatId: string): Promise<void>;
}

// Tipo de dirección, sin revelarla: útil en los registros.
export function kindOf(from: string | null | undefined): string {
  if (!from) return "none";
  if (from.endsWith("@c.us")) return "individual";
  if (from.endsWith("@lid")) return "lid";
  if (from.endsWith("@g.us")) return "group";
  if (from.endsWith("@broadcast")) return "status";
  if (from.endsWith("@newsletter")) return "newsletter";
  return "other";
}

// Identificador de correlación corto: no reversible, sin contenido ni números.
export function correlation(messageId: string): string {
  return createHash("sha256").update(messageId).digest("hex").slice(0, 10);
}

// fields: indicadores ya saneados (sin valores) y requisitos que faltan. Solo se registran.
export async function onIncoming(
  f: InboundFacts,
  deps: GatewayDeps,
  fields?: { line: string; missing: string[] },
): Promise<void> {
  const d = deps.diag;
  d.rawEventsTotal += 1;
  d.lastRawAt = new Date().toISOString();
  const corr = f.id ? correlation(f.id) : "-";
  deps.log(`stage=00_message_received kind=${kindOf(f.from)} state=${deps.state()} cid=${corr}`);
  if (fields) {
    deps.log(fields.line);
    if (fields.missing.length > 0) deps.log(`stage=01_missing requirements=${fields.missing.join(",")} cid=${corr}`);
  }

  // Clasificación primero, también con STOP: así el registro muestra la decisión real del filtro.
  const decision = classifyInbound(f, deps.policy, deps.nowSec());
  if (decision.action === "ignore") {
    d.ignoredTotal += 1;
    deps.log(`stage=01_classified cid=${corr} decision=ignore reason=${decision.reason}`);
    return;
  }
  deps.log(`stage=01_classified cid=${corr} decision=forward`);

  if (deps.isKilled()) {
    // STOP bloquea el reenvío. El mensaje queda consumido: no se reenvía aunque STOP se retire después.
    d.stopBlockedTotal += 1;
    deps.log(`stage=04_forward_skipped reason=stop_active cid=${corr}`);
    return;
  }

  const gate = forwardAllowed(deps.config, deps.nowSec());
  if (!gate.allowed) {
    deps.log(`stage=04_forward_skipped reason=${gate.reason} cid=${corr}`);
    return;
  }

  const item: ForwardItem = {
    message_id: decision.messageId,
    correlation_id: corr,
    from: decision.route,
    identity: decision.identity,
    timestamp: decision.timestampSec,
    kind: "text",
    text: decision.text,
  };
  // Ruta marcada como "entrada auténtica" ANTES de reenviar, para que la respuesta del worker (que solo existe si el
  // backend persistió el mensaje) nunca encuentre la ruta sin marcar. Si el backend NO lo admite, se retira.
  const wasKnown = deps.routes.knownRoute(decision.route);
  deps.routes.markKnown(decision.route);
  deps.log(`stage=02_backend_forward cid=${corr}`);
  const result = await deps.forward([item]);
  // Un HTTP 200 NO basta: el mensaje cuenta como reenviado solo si el backend confirma su resultado por elemento.
  const entry = result.items?.find((i) => i.correlation_id === corr) ?? result.items?.[0];
  const outcome = !result.ok ? "failed" : entry ? entry.outcome : "unknown";
  const persisted = outcome === "accepted" || outcome === "duplicate";
  if (persisted) {
    d.messagesForwardedTotal += 1;
    d.lastForwardAt = new Date().toISOString();
  } else if (!wasKnown) {
    // No admitido y la ruta no tenía entradas admitidas: no se autoriza ninguna respuesta por esa ruta.
    deps.routes.forgetKnown(decision.route);
  }
  const stage = outcome === "failed" ? "failed" : persisted ? "accepted" : "not_persisted";
  const reason = entry?.reason ? ` reason=${entry.reason}` : "";
  deps.log(`stage=03_backend_forward_${stage} http=${result.status} outcome=${outcome}${reason} cid=${corr}`);
}

// Envío de una respuesta al chat de origen. Lanza error si nada puede salir (el puente responde 409)
// o si el resultado es incierto (el puente responde 500 y el backend NO reintenta).
export async function sendText(route: string, text: string, deps: GatewayDeps): Promise<string> {
  if (!sendAllowed(deps.config, deps.nowSec())) throw new Error("send_disabled");
  if (deps.state() !== "ready") throw new Error("not_connected");
  // Atención pública: solo a rutas originadas por un evento entrante reenviado. Cualquier otra se rechaza sin enviar.
  if (deps.config.inboundMode === "public_inbound" && !deps.routes.knownRoute(route)) throw new UnresolvedRouteError();
  const chatId = deps.routes.jidFor(route);
  if (chatId === null) throw new UnresolvedRouteError();
  if (deps.transport.chatResolved) {
    // Si WhatsApp no resuelve el chat, sendMessage devuelve vacío SIN enviar. Se detecta antes, sin ambigüedad.
    let resolved = false;
    try {
      resolved = await deps.transport.chatResolved(chatId);
    } catch {
      resolved = false; // no se pudo consultar: tampoco se envía
    }
    if (!resolved) throw new ChatNotResolvedError();
  }
  let sent: Awaited<ReturnType<Transport["sendMessage"]>>;
  // Marca de inicio con margen: el mensaje propio debe tener una hora igual o posterior.
  const sinceSec = Math.floor(Date.now() / 1000) - 5;
  // La respuesta sustituye al indicador: se apaga antes de enviar y nunca bloquea el envío.
  if (deps.typingStop) await deps.typingStop(chatId).catch(() => undefined);
  try {
    sent = await deps.transport.sendMessage(chatId, text);
  } catch (err) {
    deps.diag.sendFailuresTotal += 1;
    throw err;
  }
  let id = sent?.id?._serialized;
  if (typeof id !== "string" || id.length === 0) {
    // La librería devuelve vacío DESPUÉS de haber enviado cuando no encuentra el mensaje en su almacén local
    // (addAndSendMsgToChat se ejecuta antes del lookup). Se intenta confirmar el envío con el propio chat.
    const shape = sent === undefined ? "undefined" : sent === null ? "null" : "object_without_id";
    deps.log(`stage=05_no_message_id result=${shape}`);
    const recovered = deps.transport.recoverSentId
      ? await deps.transport.recoverSentId(chatId, text, sinceSec).catch(() => null)
      : null;
    if (typeof recovered === "string" && recovered.length > 0) {
      deps.log("stage=05_send_recovered");
      id = recovered;
    } else {
      // Sin confirmación: el resultado es INCIERTO. El puente responde 500 y el backend nunca lo reenvía solo.
      deps.diag.sendFailuresTotal += 1;
      throw new Error("no_message_id");
    }
  }
  deps.diag.sendsTotal += 1;
  return id;
}
