// Política de entrada del conector. Función pura: no abre conexiones ni guarda contenido.
//
// Modo web_inbound_all (autorizado por el propietario): se procesa un mensaje NUEVO de texto en un chat
// individual, de cualquier contacto (guardado o no, con conversación previa o no).
// Siempre se descarta: mensajes propios, historial sincronizado, grupos, estados, listas, canales,
// contenido que no es texto (incluidas ediciones de mensajes, que no deben generar una segunda respuesta),
// mensajes duplicados, mensajes antiguos y direcciones que no se pueden encaminar.
//
// Modo single_chat (legado): además se descartan los contactos que ya existían al vincular.

export type IgnoreReason =
  | "history_sync"        // sincronización de historial (upsert distinto de 'notify')
  | "own_message"         // enviado desde este mismo teléfono o cuenta
  | "group"               // chat de grupo
  | "status_or_broadcast" // estados, listas de difusión
  | "newsletter"          // canales
  | "unsupported_address" // dirección que no es individual ni @lid
  | "not_text"            // imagen, audio, documento, reacción, edición de mensaje, etc.
  | "missing_fields"      // falta identificador, dirección o marca de tiempo
  | "too_old"             // mensaje antiguo: evita responder a un backlog
  | "known_contact"       // contacto existente en modo single_chat
  | "duplicate";          // mismo identificador ya visto

export interface RawInbound {
  upsertType: string;
  fromMe: boolean;
  remoteJid: string | null | undefined;
  messageId: string | null | undefined;
  timestampSec: number | null | undefined;
  kind: "text" | "other";
  text: string | null | undefined;
}

export interface PolicyState {
  // Direcciones de contactos existentes al vincular (solo se usan en modo single_chat).
  knownJids: Set<string>;
  // Identificadores ya reenviados, para no procesar dos veces el mismo mensaje.
  seenIds: Set<string>;
  // true solo en modo single_chat: los contactos existentes quedan protegidos.
  protectKnownContacts: boolean;
  // Traduce una dirección de chat al identificador que viaja al backend (número o identificador opaco).
  routeFor: (jid: string) => string | null;
}

export type Decision =
  | { action: "forward"; routeId: string; text: string; messageId: string; timestampSec: number }
  | { action: "ignore"; reason: IgnoreReason };

export const MAX_AGE_SECONDS = 120;
const INDIVIDUAL_SUFFIX = "@s.whatsapp.net";

export function normalizeIndividualJid(jid: string): string | null {
  if (!jid.endsWith(INDIVIDUAL_SUFFIX)) return null;
  const user = jid.slice(0, -INDIVIDUAL_SUFFIX.length);
  return (user.split(":")[0] ?? "") + INDIVIDUAL_SUFFIX;
}

export function phoneDigitsFromJid(jid: string): string | null {
  const base = normalizeIndividualJid(jid);
  if (base === null) return null;
  const digits = base.slice(0, -INDIVIDUAL_SUFFIX.length);
  return /^[0-9]{6,20}$/.test(digits) ? digits : null;
}

export function classifyInbound(raw: RawInbound, state: PolicyState, nowSec: number): Decision {
  // Solo 'notify' (entrega en vivo) puede despacharse. 'append' es sincronización de historial: Baileys no ofrece
  // una señal fiable que demuestre que un 'append' corresponde a una entrega nueva, aunque su marca temporal sea reciente.
  if (raw.upsertType !== "notify") return { action: "ignore", reason: "history_sync" };
  if (raw.fromMe) return { action: "ignore", reason: "own_message" };

  const jid = raw.remoteJid ?? "";
  if (!jid) return { action: "ignore", reason: "missing_fields" };
  if (jid.endsWith("@g.us")) return { action: "ignore", reason: "group" };
  if (jid === "status@broadcast" || jid.endsWith("@broadcast")) return { action: "ignore", reason: "status_or_broadcast" };
  if (jid.endsWith("@newsletter")) return { action: "ignore", reason: "newsletter" };
  if (!jid.endsWith("@lid") && normalizeIndividualJid(jid) === null) {
    return { action: "ignore", reason: "unsupported_address" };
  }

  if (raw.kind !== "text") return { action: "ignore", reason: "not_text" };
  if (!raw.messageId || raw.timestampSec == null || !raw.text) return { action: "ignore", reason: "missing_fields" };
  if (nowSec - raw.timestampSec > MAX_AGE_SECONDS || raw.timestampSec - nowSec > 60) {
    return { action: "ignore", reason: "too_old" };
  }

  const base = normalizeIndividualJid(jid);
  if (state.protectKnownContacts && base !== null && state.knownJids.has(base)) {
    return { action: "ignore", reason: "known_contact" };
  }

  const routeId = state.routeFor(jid);
  if (routeId === null) return { action: "ignore", reason: "unsupported_address" };

  if (state.seenIds.has(raw.messageId)) return { action: "ignore", reason: "duplicate" };
  state.seenIds.add(raw.messageId);
  return { action: "forward", routeId, text: raw.text, messageId: raw.messageId, timestampSec: raw.timestampSec };
}

// Registra las direcciones de la agenda o de chats existentes (solo relevante en modo single_chat).
export function rememberKnownJids(state: PolicyState, jids: Iterable<string | null | undefined>): number {
  let added = 0;
  for (const jid of jids) {
    const base = jid ? normalizeIndividualJid(jid) : null;
    if (base !== null && !state.knownJids.has(base)) {
      state.knownJids.add(base);
      added += 1;
    }
  }
  return added;
}
