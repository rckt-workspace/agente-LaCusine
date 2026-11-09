// Configuración del conector whatsapp-web.js desde variables de entorno. Falla cerrado ante valores inseguros.
//
// Modos:
//   observe  Conecta, registra eventos y clasifica. NUNCA reenvía al backend ni envía. Ventana obligatoria.
//   live     Reenvía al backend y permite enviar SOLO con WEB_BRIDGE_AUTO_REPLY=true.
export type Mode = "observe" | "live";
// public_inbound: atención pública sin ventana. Solo con WEB_BRIDGE_AUTO_REPLY=true y el token exacto
// WEB_BRIDGE_PUBLIC_CONFIRMATION (mismo valor que PUBLIC_INBOUND_CONFIRMATION en el backend).
export type InboundMode = "single_chat" | "web_inbound_all" | "public_inbound";
export const PUBLIC_INBOUND_CONFIRMATION = "ACTIVAR_ENTRADA_PUBLICA_WEB_BRIDGE";

export interface WebjsConfig {
  secret: string;
  backendUrl: string;
  port: number;
  mode: Mode;
  autoReply: boolean;
  inboundMode: InboundMode;
  // Ventana universal (live + web_inbound_all): instante Unix en que el proceso se cierra.
  inboundUntil?: number;
  // Observación con duración absoluta: instante Unix en que el proceso se cierra.
  observeUntil?: number;
  // Observación con duración relativa: la ventana empieza a contar cuando la sesión confirma "ready".
  observeSeconds?: number;
  // Plazo máximo para que "ready" llegue tras "authenticated". Vencido el plazo, el conector falla explícitamente.
  readyTimeoutSec: number;
  authDir: string;
  stateDir: string;
  logDir: string;
  chromePath?: string;
}

export const READY_TIMEOUT_DEFAULT_SECONDS = 120;

export const WINDOW_MAX_SECONDS = 900;

export function loadConfig(env: NodeJS.ProcessEnv = process.env, nowSec: number = Math.floor(Date.now() / 1000)): WebjsConfig {
  const secret = (env.WEB_BRIDGE_SECRET ?? "").trim();
  if (secret.length < 32) throw new Error("WEB_BRIDGE_SECRET debe tener al menos 32 caracteres");

  const backendUrl = (env.WEB_BRIDGE_BACKEND_URL ?? "http://127.0.0.1:8000").trim();
  const parsed = new URL(backendUrl);
  const loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost";
  if (!loopback && parsed.protocol !== "https:") throw new Error("WEB_BRIDGE_BACKEND_URL debe ser loopback o HTTPS");

  const port = Number(env.WEB_BRIDGE_PORT ?? "8787");
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("WEB_BRIDGE_PORT no válido");

  const modeRaw = (env.WEBJS_MODE ?? "live").trim().toLowerCase();
  if (modeRaw !== "observe" && modeRaw !== "live") throw new Error("WEBJS_MODE no válido (observe | live)");
  const mode: Mode = modeRaw;

  const autoReply = (env.WEB_BRIDGE_AUTO_REPLY ?? "false").trim().toLowerCase() === "true";

  const inboundRaw = (env.WEB_BRIDGE_INBOUND_MODE ?? "single_chat").trim().toLowerCase();
  if (inboundRaw !== "single_chat" && inboundRaw !== "web_inbound_all" && inboundRaw !== "public_inbound") {
    throw new Error("WEB_BRIDGE_INBOUND_MODE no válido");
  }
  const inboundMode: InboundMode = inboundRaw;

  const readyRaw = (env.WEBJS_READY_TIMEOUT_SEC ?? "").trim();
  const readyTimeoutSec = readyRaw ? parseBounded(readyRaw, 30, 600, "WEBJS_READY_TIMEOUT_SEC") : READY_TIMEOUT_DEFAULT_SECONDS;

  let observeUntil: number | undefined;
  let observeSeconds: number | undefined;
  let inboundUntil: number | undefined;
  if (mode === "observe") {
    if (autoReply) throw new Error("WEBJS_MODE=observe no admite WEB_BRIDGE_AUTO_REPLY=true");
    const secondsRaw = (env.WEBJS_OBSERVE_SECONDS ?? "").trim();
    if (secondsRaw) observeSeconds = parseBounded(secondsRaw, 60, 900, "WEBJS_OBSERVE_SECONDS");
    else observeUntil = windowEnd(env.WEBJS_OBSERVE_UNTIL, nowSec, "WEBJS_OBSERVE_UNTIL");
  } else if (inboundMode === "web_inbound_all") {
    if (!autoReply) throw new Error("web_inbound_all requiere WEB_BRIDGE_AUTO_REPLY=true");
    inboundUntil = windowEnd(env.WEB_BRIDGE_INBOUND_UNTIL, nowSec, "WEB_BRIDGE_INBOUND_UNTIL");
  } else if (inboundMode === "single_chat" && (env.WEB_BRIDGE_INBOUND_UNTIL ?? "").trim() !== "") {
    // Un solo chat: la lista la controla el backend. Este instante solo CIERRA el proceso a la hora indicada.
    inboundUntil = windowEnd(env.WEB_BRIDGE_INBOUND_UNTIL, nowSec, "WEB_BRIDGE_INBOUND_UNTIL");
  } else if (inboundMode === "public_inbound") {
    if (!autoReply) throw new Error("public_inbound requiere WEB_BRIDGE_AUTO_REPLY=true");
    if ((env.WEB_BRIDGE_PUBLIC_CONFIRMATION ?? "").trim() !== PUBLIC_INBOUND_CONFIRMATION) {
      throw new Error("public_inbound requiere WEB_BRIDGE_PUBLIC_CONFIRMATION exacta");
    }
    if (env.WEB_BRIDGE_INBOUND_UNTIL) throw new Error("public_inbound no admite WEB_BRIDGE_INBOUND_UNTIL (sin ventana)");
  }

  const chromePath = (env.WEBJS_CHROME_PATH ?? "").trim() || undefined;

  return {
    secret,
    backendUrl,
    port,
    mode,
    autoReply,
    inboundMode,
    inboundUntil,
    observeUntil,
    observeSeconds,
    readyTimeoutSec,
    authDir: env.WEBJS_AUTH_DIR ?? ".wwebjs_auth",
    stateDir: env.WEBJS_STATE_DIR ?? "state",
    logDir: env.WEBJS_LOG_DIR ?? "state/logs",
    chromePath,
  };
}

function parseBounded(raw: string, min: number, max: number, name: string): number {
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`${name} no válido (${min}-${max})`);
  return value;
}

function windowEnd(raw: string | undefined, nowSec: number, name: string): number {
  const until = Number((raw ?? "").trim());
  if (!Number.isInteger(until) || until <= nowSec) throw new Error(`${name} ausente, expirado o inválido`);
  if (until - nowSec > WINDOW_MAX_SECONDS) throw new Error(`${name}: la ventana no puede durar más de 15 minutos`);
  return until;
}

// Instante en que el proceso debe cerrarse por sí mismo (o undefined si no tiene ventana).
export function deadlineOf(config: WebjsConfig): number | undefined {
  return config.mode === "observe" ? config.observeUntil : config.inboundUntil;
}

// Ventana universal abierta. En modo de un solo chat no aplica: la lista la controla el backend.
export function inboundWindowOpen(config: WebjsConfig, nowSec: number = Math.floor(Date.now() / 1000)): boolean {
  if (config.inboundMode === "public_inbound") return true; // sin ventana: solo la confirmación del arranque
  if (config.inboundMode !== "web_inbound_all") return true;
  return config.inboundUntil !== undefined && nowSec < config.inboundUntil;
}

// Decisión única de reenvío al backend. Función pura.
export function forwardAllowed(
  config: WebjsConfig,
  nowSec: number = Math.floor(Date.now() / 1000),
): { allowed: true } | { allowed: false; reason: "observe_only" | "auto_reply_off" | "window_closed" } {
  if (config.mode === "observe") return { allowed: false, reason: "observe_only" };
  if (!config.autoReply) return { allowed: false, reason: "auto_reply_off" };
  if (!inboundWindowOpen(config, nowSec)) return { allowed: false, reason: "window_closed" };
  return { allowed: true };
}

// Envío permitido: solo en vivo, con respuestas activadas y ventana abierta.
export function sendAllowed(config: WebjsConfig, nowSec: number = Math.floor(Date.now() / 1000)): boolean {
  return forwardAllowed(config, nowSec).allowed;
}
