// Servidor HTTP local del conector. Escucha solo en 127.0.0.1. Cada ruta exige firma HMAC.
//
//   GET  /status   -> estado de conexión y modo (sin datos personales)              dirección "status"
//   POST /send     -> enviar texto a un número, solo si WEB_BRIDGE_AUTO_REPLY=true   dirección "send"
//   POST /kill     -> apagado inmediato: cierra la sesión y bloquea envíos           dirección "control"
//   POST /unlink   -> desvincula ESTE dispositivo (logout). No borra la cuenta.     dirección "control"
import http from "node:http";
import { verify } from "./hmac.js";

export interface BridgeDeps {
  secret: string;
  isEnabled: () => boolean;
  isKilled: () => boolean;
  // Instante de caducidad de la ventana universal (Unix, segundos), o null si no aplica. Solo para verificación.
  inboundUntil?: () => number | null;
  connectionState: () => string;
  // Contadores de solo lectura (sin contenido ni identificadores). Se añaden a /status.
  diagnostics?: () => Record<string, unknown>;
  sendText: (phoneDigits: string, text: string) => Promise<string>;
  onKill: () => Promise<void>;
  onUnlink: () => Promise<void>;
  maxBodyBytes?: number;
  nowSec?: () => number;
}

const MAX_BODY_BYTES_DEFAULT = 16 * 1024;
const PHONE_PATTERN = /^[0-9]{6,20}$/;
const MAX_TEXT_CHARS = 4096;

function readBody(req: http.IncomingMessage, limit: number): Promise<Buffer | null> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    let total = 0;
    req.on("data", (chunk: Buffer) => {
      total += chunk.length;
      if (total > limit) {
        resolve(null);
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", () => resolve(null));
  });
}

function respond(res: http.ServerResponse, status: number, body: unknown): void {
  const data = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) });
  res.end(data);
}

export function createBridgeServer(deps: BridgeDeps): http.Server {
  const limit = deps.maxBodyBytes ?? MAX_BODY_BYTES_DEFAULT;
  const now = deps.nowSec ?? (() => Math.floor(Date.now() / 1000));
  // Idempotencia: el mismo idempotency_key nunca envía dos veces.
  const sentByKey = new Map<string, string>();

  return http.createServer(async (req, res) => {
    const url = req.url ?? "";
    const method = req.method ?? "";
    const routes: Record<string, { method: string; direction: string }> = {
      "/status": { method: "GET", direction: "status" },
      "/send": { method: "POST", direction: "send" },
      "/kill": { method: "POST", direction: "control" },
      "/unlink": { method: "POST", direction: "control" },
    };
    const route = routes[url.split("?")[0] ?? ""];
    if (!route || route.method !== method) {
      respond(res, 404, { error: "not_found" });
      return;
    }

    const body = await readBody(req, limit);
    if (body === null) {
      respond(res, 413, { error: "payload_too_large" });
      return;
    }
    const stamp = String(req.headers["x-bridge-timestamp"] ?? "");
    const signature = req.headers["x-bridge-signature"] as string | undefined;
    if (!verify(deps.secret, route.direction, stamp, body, signature, now())) {
      respond(res, 401, { error: "invalid_signature" });
      return;
    }

    if (url.startsWith("/status")) {
      respond(res, 200, {
        connection: deps.connectionState(),
        auto_reply: deps.isEnabled(),
        killed: deps.isKilled(),
        inbound_until: deps.inboundUntil ? deps.inboundUntil() : null,
        ...(deps.diagnostics ? deps.diagnostics() : {}),
      });
      return;
    }
    if (url.startsWith("/kill")) {
      await deps.onKill();
      respond(res, 200, { killed: true });
      return;
    }
    if (url.startsWith("/unlink")) {
      await deps.onUnlink();
      respond(res, 200, { unlinked: true });
      return;
    }

    // /send
    if (deps.isKilled()) {
      respond(res, 409, { error: "killed" });
      return;
    }
    if (!deps.isEnabled()) {
      // Las respuestas automáticas no están autorizadas: no se toca WhatsApp.
      respond(res, 409, { error: "send_disabled" });
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(body.toString("utf8"));
    } catch {
      respond(res, 400, { error: "invalid_json" });
      return;
    }
    const data = (parsed ?? {}) as Record<string, unknown>;
    const to = typeof data.to === "string" ? data.to : "";
    const text = typeof data.text === "string" ? data.text : "";
    const key = typeof data.idempotency_key === "string" ? data.idempotency_key : "";
    if (!PHONE_PATTERN.test(to) || text.length < 1 || text.length > MAX_TEXT_CHARS || key.length < 1 || key.length > 200) {
      respond(res, 400, { error: "invalid_payload" });
      return;
    }
    const previous = sentByKey.get(key);
    if (previous) {
      respond(res, 200, { message_id: previous, duplicate: true });
      return;
    }
    try {
      const messageId = await deps.sendText(to, text);
      sentByKey.set(key, messageId);
      respond(res, 200, { message_id: messageId });
    } catch (err) {
      if (err instanceof Error && err.name === "UnresolvedRouteError") {
        // Nada salió: el backend lo trata como rechazo definitivo, sin reintentos.
        respond(res, 422, { error: "unresolved_route" });
        return;
      }
      // El detalle del error puede contener datos: no se devuelve. El backend lo trata como incierto.
      respond(res, 500, { error: "send_failed" });
    }
  });
}
