// Configuración del conector desde variables de entorno. Falla cerrado ante cualquier valor inseguro.
export interface ConnectorConfig {
  secret: string;
  backendUrl: string;
  port: number;
  autoReply: boolean;
  authDir: string;
  stateDir: string;
  // single_chat: contactos existentes protegidos (legado). web_inbound_all: todos los chats individuales nuevos.
  inboundMode: "single_chat" | "web_inbound_all";
  inboundUntil?: number;
  // Observación pasiva: conecta y registra eventos brutos, pero nunca reenvía al backend ni envía.
  observeOnly: boolean;
  observeUntil?: number;
}

export const INBOUND_ALL_MAX_SECONDS = 900;
export const OBSERVE_MAX_SECONDS = 900;

// Decisión única de reenvío. Función pura. Orden: observación, respuestas desactivadas, ventana cerrada.
export function forwardAllowed(config: ConnectorConfig, nowSec: number = Math.floor(Date.now() / 1000)):
  { allowed: true } | { allowed: false; reason: "observe_only" | "auto_reply_off" | "window_closed" } {
  if (config.observeOnly) return { allowed: false, reason: "observe_only" };
  if (!config.autoReply) return { allowed: false, reason: "auto_reply_off" };
  if (!inboundWindowOpen(config, nowSec)) return { allowed: false, reason: "window_closed" };
  return { allowed: true };
}

// Ventana universal: pasado el instante de caducidad no se reenvía ni se envía nada, aunque el proceso siga abierto.
export function inboundWindowOpen(config: ConnectorConfig, nowSec: number = Math.floor(Date.now() / 1000)): boolean {
  if (config.inboundMode !== "web_inbound_all") return true;
  return config.inboundUntil !== undefined && nowSec < config.inboundUntil;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env, nowSec: number = Math.floor(Date.now() / 1000)): ConnectorConfig {
  const secret = (env.WEB_BRIDGE_SECRET ?? "").trim();
  if (secret.length < 32) {
    throw new Error("WEB_BRIDGE_SECRET debe tener al menos 32 caracteres");
  }
  const backendUrl = (env.WEB_BRIDGE_BACKEND_URL ?? "http://127.0.0.1:8000").trim();
  const parsed = new URL(backendUrl);
  const loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost";
  if (!loopback && parsed.protocol !== "https:") {
    throw new Error("WEB_BRIDGE_BACKEND_URL debe ser loopback o HTTPS");
  }
  const port = Number(env.WEB_BRIDGE_PORT ?? "8787");
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error("WEB_BRIDGE_PORT no válido");
  }
  const autoReply = (env.WEB_BRIDGE_AUTO_REPLY ?? "false").trim().toLowerCase() === "true";

  const inboundRaw = (env.WEB_BRIDGE_INBOUND_MODE ?? "single_chat").trim().toLowerCase();
  if (inboundRaw !== "single_chat" && inboundRaw !== "web_inbound_all") {
    throw new Error("WEB_BRIDGE_INBOUND_MODE no válido");
  }
  let inboundUntil: number | undefined;
  if (inboundRaw === "web_inbound_all") {
    inboundUntil = Number((env.WEB_BRIDGE_INBOUND_UNTIL ?? "").trim());
    if (!Number.isInteger(inboundUntil) || inboundUntil <= nowSec) {
      throw new Error("WEB_BRIDGE_INBOUND_UNTIL ausente, expirado o inválido");
    }
    if (inboundUntil - nowSec > INBOUND_ALL_MAX_SECONDS) {
      throw new Error("la ventana universal no puede durar más de 15 minutos");
    }
  }

  const observeOnly = (env.WEB_BRIDGE_OBSERVE_ONLY ?? "false").trim().toLowerCase() === "true";
  let observeUntil: number | undefined;
  if (observeOnly) {
    // La observación nunca puede convivir con respuestas automáticas, aunque se pidan por error.
    if (autoReply) throw new Error("WEB_BRIDGE_OBSERVE_ONLY no admite WEB_BRIDGE_AUTO_REPLY=true");
    observeUntil = Number((env.WEB_BRIDGE_OBSERVE_UNTIL ?? "").trim());
    if (!Number.isInteger(observeUntil) || observeUntil <= nowSec) {
      throw new Error("WEB_BRIDGE_OBSERVE_UNTIL ausente, expirado o inválido");
    }
    if (observeUntil - nowSec > OBSERVE_MAX_SECONDS) {
      throw new Error("la observación no puede durar más de 15 minutos");
    }
  }

  return {
    observeOnly,
    observeUntil,
    secret,
    backendUrl,
    port,
    autoReply,
    authDir: env.WEB_BRIDGE_AUTH_DIR ?? ".auth",
    stateDir: env.WEB_BRIDGE_STATE_DIR ?? "state",
    inboundMode: inboundRaw === "web_inbound_all" ? "web_inbound_all" : "single_chat",
    inboundUntil,
  };
}
