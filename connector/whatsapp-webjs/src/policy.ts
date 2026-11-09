// Política de entrada de whatsapp-web.js. Función pura: no abre conexiones ni guarda contenido.
//
// Se procesa solo un mensaje NUEVO de texto en un chat individual (@c.us o @lid), de cualquier contacto,
// guardado o no. Siempre se descarta: mensajes propios, grupos, estados, difusiones, canales, contenido que no
// es texto, datos incompletos, mensajes antiguos o con marca de tiempo futura, y duplicados.
// No se exige ninguna frase de activación.

export type IgnoreReason =
  | "own_message"
  | "group"
  | "status_or_broadcast"
  | "newsletter"
  | "unsupported_address"
  | "not_text"
  | "missing_fields"
  | "too_old"
  | "future_timestamp"
  | "duplicate"
  | "identity_unverified";

export interface InboundFacts {
  fromMe: boolean;
  from: string | null | undefined;
  type: string | null | undefined;
  body: string | null | undefined;
  id: string | null | undefined;
  timestampSec: number | null | undefined;
  broadcast?: boolean;
  isStatus?: boolean;
  // Número telefónico verificado por WhatsApp para un chat @lid (sin validar aquí). Para @c.us se deriva del chat.
  identity?: string | null;
}

export type Decision =
  | {
      action: "forward";
      route: string;
      identity: string | null;
      text: string;
      messageId: string;
      timestampSec: number;
      chatId: string;
    }
  | { action: "ignore"; reason: IgnoreReason };

const PHONE_DIGITS = /^[0-9]{6,20}$/;

// Conjunto acotado: al superar el máximo descarta el identificador más antiguo.
export class BoundedSet {
  private readonly items = new Set<string>();
  constructor(private readonly max: number) {}
  has(value: string): boolean {
    return this.items.has(value);
  }
  add(value: string): void {
    this.items.add(value);
    if (this.items.size > this.max) {
      const oldest = this.items.values().next().value;
      if (oldest !== undefined) this.items.delete(oldest);
    }
  }
}

export interface PolicyState {
  seen: BoundedSet;
  routeFor: (chatId: string) => string | null;
  // Atención pública: no se exige identidad telefónica para responder (la ruta procede de un evento entrante).
  publicInbound?: boolean;
}

export const MAX_AGE_SECONDS = 120;
export const MAX_FUTURE_SKEW_SECONDS = 60;

export function classifyInbound(f: InboundFacts, state: PolicyState, nowSec: number): Decision {
  if (f.fromMe) return { action: "ignore", reason: "own_message" };
  const chatId = f.from ?? "";
  if (!chatId) return { action: "ignore", reason: "missing_fields" };
  if (f.isStatus || f.broadcast || chatId === "status@broadcast" || chatId.endsWith("@broadcast")) {
    return { action: "ignore", reason: "status_or_broadcast" };
  }
  if (chatId.endsWith("@g.us")) return { action: "ignore", reason: "group" };
  if (chatId.endsWith("@newsletter")) return { action: "ignore", reason: "newsletter" };
  if (!chatId.endsWith("@c.us") && !chatId.endsWith("@lid")) return { action: "ignore", reason: "unsupported_address" };

  if (f.type !== "chat" || !f.body || f.body.trim().length === 0) return { action: "ignore", reason: "not_text" };
  const messageId = f.id ?? "";
  const ts = f.timestampSec;
  if (!messageId || ts == null || !Number.isFinite(ts)) return { action: "ignore", reason: "missing_fields" };
  if (nowSec - ts > MAX_AGE_SECONDS) return { action: "ignore", reason: "too_old" };
  if (ts - nowSec > MAX_FUTURE_SKEW_SECONDS) return { action: "ignore", reason: "future_timestamp" };

  const route = state.routeFor(chatId);
  if (route === null) return { action: "ignore", reason: "unsupported_address" };
  // Identidad autorizable: en @c.us el propio chat es el número; en @lid debe venir verificada por WhatsApp.
  // Nunca se deduce de los dígitos de un @lid ni del texto del mensaje.
  const identity = chatId.endsWith("@c.us") ? chatId.slice(0, -"@c.us".length) : f.identity ?? null;
  const verified = identity !== null && PHONE_DIGITS.test(identity);
  if (!verified && !state.publicInbound) return { action: "ignore", reason: "identity_unverified" };
  if (state.seen.has(messageId)) return { action: "ignore", reason: "duplicate" };
  state.seen.add(messageId);
  return { action: "forward", route, identity: verified ? identity : null, text: f.body, messageId, timestampSec: ts, chatId };
}
