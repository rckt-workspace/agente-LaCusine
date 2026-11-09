// Verificación de identidad de un chat @lid con el mapeo que mantiene WhatsApp (client.getContactLidAndPhone).
// No se usa el número declarado por el mensaje, ni los dígitos del @lid, ni la agenda de contactos.
// Cualquier respuesta ambigua, contradictoria, ausente o fuera de plazo se rechaza.
export type IdentityOutcome =
  | { ok: true; phone: string }
  | { ok: false; reason: "lookup_failed" | "lookup_timeout" | "ambiguous" | "lid_mismatch" | "no_phone_mapping" | "phone_format" };

export type LidLookup = (userIds: string[]) => Promise<Array<{ lid?: string | null; pn?: string | null }>>;

const PN_PATTERN = /^([0-9]{6,20})@c\.us$/;

export async function verifyLidIdentity(chatId: string, lookup: LidLookup, timeoutMs = 5_000): Promise<IdentityOutcome> {
  let answer: Array<{ lid?: string | null; pn?: string | null }>;
  try {
    answer = await withTimeout(lookup([chatId]), timeoutMs);
  } catch (err) {
    return { ok: false, reason: err instanceof TimeoutError ? "lookup_timeout" : "lookup_failed" };
  }
  if (!Array.isArray(answer) || answer.length !== 1) return { ok: false, reason: "ambiguous" };
  const [entry] = answer;
  if (!entry || entry.lid !== chatId) return { ok: false, reason: "lid_mismatch" };
  if (!entry.pn) return { ok: false, reason: "no_phone_mapping" };
  const match = PN_PATTERN.exec(entry.pn);
  if (!match) return { ok: false, reason: "phone_format" };
  return { ok: true, phone: match[1] as string };
}

class TimeoutError extends Error {}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const limit = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new TimeoutError("timeout")), ms);
  });
  return Promise.race([promise, limit]).finally(() => clearTimeout(timer));
}
