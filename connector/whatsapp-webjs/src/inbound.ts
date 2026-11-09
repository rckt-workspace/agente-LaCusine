// Normalización del objeto Message de whatsapp-web.js en InboundFacts. Función pura.
//
// Reglas:
//   * El identificador sale de msg.id._serialized si existe. Si no existe, se reconstruye SOLO desde los campos
//     documentados del ID (fromMe, remote, id) con el formato de la librería: "<fromMe>_<remote>_<id>".
//     Si faltan campos, no hay identificador: nunca se inventa uno.
//   * El chat es msg.from (nunca msg.to como sustituto).
//   * El timestamp son segundos Unix: un valor fuera de ese rango se considera inválido, no se corrige.
//   * Los indicadores son booleanos y tipos: nunca contienen el ID, el número, el texto ni el contacto.
import type { InboundFacts } from "./policy.js";
import { kindOf } from "./gateway.js";

export type IdSource = "serialized" | "dollar" | "reconstructed" | "none";
const SERIALIZED_ID_FORMAT = /^(true|false)_[^_\s]+_[^\s]+$/;

export interface FieldIndicators {
  from_me: boolean;
  has_chat_id: boolean;
  chat_kind: string;
  has_message_id: boolean;
  id_source: IdSource;
  has_body: boolean;
  is_text: boolean;
  timestamp_valid: boolean;
}

const MIN_UNIX_SECONDS = 1_000_000_000; // 2001
const MAX_UNIX_SECONDS = 100_000_000_000; // año 5138: por encima suele ser milisegundos

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function messageIdOf(id: any): { value: string | null; source: IdSource } {
  if (typeof id === "string") return { value: nonEmptyString(id), source: nonEmptyString(id) ? "serialized" : "none" };
  const serialized = nonEmptyString(id?._serialized);
  if (serialized) return { value: serialized, source: "serialized" };
  // Variante conocida en cambios de WhatsApp Web: el ID serializado llega como "$1". Solo se acepta con el
  // formato exacto "<fromMe>_<remote>_<id>"; cualquier otra forma se ignora.
  const dollar = nonEmptyString(id?.$1);
  if (dollar && SERIALIZED_ID_FORMAT.test(dollar)) return { value: dollar, source: "dollar" };
  const remote = nonEmptyString(id?.remote) ?? nonEmptyString(id?.remote?._serialized);
  const local = nonEmptyString(id?.id);
  if (remote && local && typeof id?.fromMe === "boolean") {
    return { value: `${id.fromMe}_${remote}_${local}`, source: "reconstructed" };
  }
  return { value: null, source: "none" };
}

export function normalizeMessage(msg: any): { facts: InboundFacts; indicators: FieldIndicators } {
  const from = nonEmptyString(typeof msg?.from === "object" && msg?.from !== null ? msg.from._serialized : msg?.from);
  const id = messageIdOf(msg?.id);
  const t = msg?.timestamp;
  const timestampValid = typeof t === "number" && Number.isFinite(t) && t >= MIN_UNIX_SECONDS && t < MAX_UNIX_SECONDS;
  const body = typeof msg?.body === "string" ? msg.body : undefined;

  const facts: InboundFacts = {
    fromMe: Boolean(msg?.fromMe),
    from,
    type: typeof msg?.type === "string" ? msg.type : undefined,
    body,
    id: id.value,
    timestampSec: timestampValid ? t : null,
    broadcast: Boolean(msg?.broadcast),
    isStatus: Boolean(msg?.isStatus),
  };
  const indicators: FieldIndicators = {
    from_me: facts.fromMe,
    has_chat_id: from !== null,
    chat_kind: kindOf(from),
    has_message_id: id.value !== null,
    id_source: id.source,
    has_body: typeof body === "string" && body.trim().length > 0,
    is_text: facts.type === "chat",
    timestamp_valid: timestampValid,
  };
  return { facts, indicators };
}

// Requisitos que, si fallan, el clasificador resuelve como "missing_fields". Solo nombres, nunca valores.
export function missingRequirements(ind: FieldIndicators): string[] {
  const missing: string[] = [];
  if (!ind.has_chat_id) missing.push("chat_id");
  if (!ind.has_message_id) missing.push("message_id");
  if (!ind.timestamp_valid) missing.push("timestamp");
  return missing;
}

// Línea de diagnóstico sin contenido: solo indicadores booleanos y tipos.
export function fieldsLine(ind: FieldIndicators): string {
  return (
    `stage=01_fields from_me=${ind.from_me} has_chat_id=${ind.has_chat_id} chat_kind=${ind.chat_kind} ` +
    `has_message_id=${ind.has_message_id} id_source=${ind.id_source} has_body=${ind.has_body} ` +
    `is_text=${ind.is_text} timestamp_valid=${ind.timestamp_valid}`
  );
}
