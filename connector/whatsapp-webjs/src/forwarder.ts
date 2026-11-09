// Reenvío firmado de mensajes aprobados por la política hacia FastAPI (/internal/web-bridge/inbound).
import { sign } from "./hmac.js";

export interface ForwardItem {
  message_id: string;
  correlation_id: string;
  // Ruta de respuesta: chat original (dígitos para @c.us, identificador opaco para @lid).
  from: string;
  // Número telefónico verificado por WhatsApp: es lo que el backend compara con la lista permitida.
  // null solo en atención pública (inbound_mode=public_inbound): no se exige número para responder.
  identity: string | null;
  timestamp: number;
  kind: "text";
  text: string;
}

// Resultado por elemento que devuelve el backend (campo "items"). Un HTTP 200 NO implica que el mensaje se guardara.
export interface ItemOutcome {
  correlation_id: string;
  outcome: "accepted" | "duplicate" | "conflict" | "rejected" | string;
  reason?: string | null;
}

export interface ForwardResult {
  ok: boolean;
  status: number;
  items?: ItemOutcome[];
}

export async function forwardToBackend(
  backendUrl: string,
  secret: string,
  items: ForwardItem[],
  timeoutMs = 10_000,
): Promise<ForwardResult> {
  if (items.length === 0) return { ok: true, status: 204 };
  const body = JSON.stringify({ messages: items });
  const stamp = String(Math.floor(Date.now() / 1000));
  try {
    const response = await fetch(`${backendUrl.replace(/\/$/, "")}/internal/web-bridge/inbound`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-Timestamp": stamp,
        "X-Bridge-Signature": sign(secret, "inbound", stamp, body),
      },
      body,
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) return { ok: false, status: response.status };
    // Solo se leen códigos de resultado, nunca el contenido del mensaje ni el cuerpo de error.
    let items: ItemOutcome[] | undefined;
    try {
      const body = (await response.json()) as { items?: unknown };
      if (Array.isArray(body?.items)) {
        items = body.items.filter(
          (i): i is ItemOutcome => typeof i === "object" && i !== null && typeof (i as ItemOutcome).outcome === "string",
        );
      }
    } catch {
      items = undefined; // respuesta sin JSON: el conector no puede afirmar que se guardó
    }
    return { ok: true, status: response.status, items };
  } catch {
    // Sin detalle del error: puede incluir la URL o datos del mensaje.
    return { ok: false, status: 0 };
  }
}
