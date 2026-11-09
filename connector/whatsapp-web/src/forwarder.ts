// Reenvío firmado de mensajes aprobados por la política hacia FastAPI (/internal/web-bridge/inbound).
import { sign } from "./hmac.js";

export interface ForwardItem {
  message_id: string;
  correlation_id: string;
  from: string;
  timestamp: number;
  kind: "text";
  text: string;
}

export interface ForwardResult {
  ok: boolean;
  status: number;
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
    return { ok: response.ok, status: response.status };
  } catch {
    // Sin detalle del error: puede incluir la URL o datos del mensaje.
    return { ok: false, status: 0 };
  }
}
