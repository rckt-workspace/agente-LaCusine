// Firma HMAC-SHA256 entre el conector y el backend. Mismo formato que app/integrations/web_bridge.py:
//   mensaje = "<dirección>.<marca de tiempo>." + cuerpo
import { createHmac, timingSafeEqual } from "node:crypto";

export const SIGNATURE_WINDOW_SECONDS = 300;

export function sign(secret: string, direction: string, timestamp: string, body: string | Buffer): string {
  const prefix = Buffer.from(`${direction}.${timestamp}.`, "utf8");
  const payload = Buffer.isBuffer(body) ? body : Buffer.from(body, "utf8");
  return createHmac("sha256", secret).update(prefix).update(payload).digest("hex");
}

export function verify(
  secret: string,
  direction: string,
  timestamp: string,
  body: string | Buffer,
  signature: string | undefined,
  nowSec: number = Math.floor(Date.now() / 1000),
): boolean {
  const stamp = Number(timestamp);
  if (!Number.isInteger(stamp) || Math.abs(nowSec - stamp) > SIGNATURE_WINDOW_SECONDS) return false;
  if (!signature) return false;
  const expected = Buffer.from(sign(secret, direction, timestamp, body), "utf8");
  const received = Buffer.from(signature, "utf8");
  return expected.length === received.length && timingSafeEqual(expected, received);
}
