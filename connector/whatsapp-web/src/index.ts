// Conector WhatsApp Web (dispositivo vinculado, Baileys). EXPERIMENTAL y NO oficial. Ver README.md.
//
// Garantías del proceso:
//   * Nada se reenvía al backend hasta que termine la sincronización inicial (o pasen 120 s).
//   * Los registros contienen solo estados, motivos, contadores y un identificador de correlación truncado. Nunca
//     texto, números completos, nombres ni identificadores de chat o de mensaje sin anonimizar.
//   * Con WEB_BRIDGE_AUTO_REPLY distinto de "true", no se reenvía nada al backend ni se envía nada.
//   * La ventana universal caduca: al vencer no se reenvía ni se envía, aunque el proceso siga abierto.
//   * El apagado es inmediato: POST /kill, o la existencia del archivo state/STOP.
//   * Cada evento bruto 'messages.upsert' se registra ANTES de cualquier filtro o comprobación de STOP.
//   * "ready" solo es verdadero con el socket abierto y el calentamiento cumplido; se pone a falso al cerrarse.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import makeWASocket, { DisconnectReason, useMultiFileAuthState } from "@whiskeysockets/baileys";
import { pino } from "pino";
import qrcode from "qrcode-terminal";
import { createBridgeServer } from "./bridge.js";
import { forwardAllowed, inboundWindowOpen, loadConfig } from "./config.js";
import { forwardToBackend, type ForwardItem } from "./forwarder.js";
import { classifyInbound, rememberKnownJids, type PolicyState, type RawInbound } from "./policy.js";
import { releaseLock, acquireLock } from "./lock.js";
import { routeTableFor, UnresolvedRouteError } from "./routes.js";

const config = loadConfig();
mkdirSync(config.stateDir, { recursive: true });
const stopFile = join(config.stateDir, "STOP");
const log = pino({ level: "info" });
const routes = routeTableFor(config.stateDir);
const observeLock = join(config.stateDir, "observe.lock");

// Cierre limpio: cierra el socket, el servidor local y libera el cerrojo. No borra .auth ni STOP.
function shutdown(code: number): void {
  try {
    void sock?.end(undefined);
  } catch {
    // Ya cerrado.
  }
  if (config.observeOnly) releaseLock(observeLock);
  server.close();
  process.exit(code);
}

if (config.observeOnly) {
  // Un único conector por sesión: si otro proceso ya la usa, este no arranca.
  const lock = acquireLock(observeLock);
  if (!lock.ok) {
    log.error("stage=observe refused reason=lock_held (otro conector usa la sesión)");
    process.exit(1);
  }
  log.info(`stage=observe state=started hasta_unix=${config.observeUntil} reenvio=bloqueado envio=bloqueado`);
}

const policy: PolicyState = {
  knownJids: new Set(),
  seenIds: new Set(),
  protectKnownContacts: config.inboundMode === "single_chat",
  routeFor: (jid) => routes.routeFor(jid),
};
const WARMUP_MS = 120_000;
let connection = "connecting";
let ready = false;
let openedAt: number | null = null;
let killedInMemory = false;
let sock: any = null;
const isKilled = (): boolean => killedInMemory || existsSync(stopFile);

// Contadores de solo lectura. Nunca contienen contenido ni identificadores.
const diag = {
  rawUpsertsTotal: 0,
  notifyReceivedTotal: 0,
  appendIgnoredTotal: 0,
  stopBlockedTotal: 0,
  messagesForwardedTotal: 0,
  handlerErrorsTotal: 0,
  lastRawUpsertAt: null as string | null,
  lastForwardAt: null as string | null,
};

// Identificador de correlación corto: no reversible, sin contenido ni números.
const cid = (messageId: string): string => createHash("sha256").update(messageId).digest("hex").slice(0, 10);

// WhatsApp envuelve con frecuencia el texto en mensajes efímeros o de visualización única. Se desenvuelven para
// no tratar un texto normal como "no texto".
function unwrapContent(content: any): any {
  let current = content;
  for (let depth = 0; depth < 4; depth++) {
    const inner =
      current?.ephemeralMessage?.message ??
      current?.viewOnceMessage?.message ??
      current?.viewOnceMessageV2?.message ??
      current?.viewOnceMessageV2Extension?.message;
    if (!inner) break;
    current = inner;
  }
  return current;
}

function toRaw(msg: any, upsertType: string): RawInbound {
  const content = unwrapContent(msg?.message);
  const text: string | null = content?.conversation ?? content?.extendedTextMessage?.text ?? null;
  const ts = msg?.messageTimestamp;
  const timestampSec = ts == null ? null : Number(typeof ts === "object" && "toNumber" in ts ? ts.toNumber() : ts);
  return {
    upsertType,
    fromMe: Boolean(msg?.key?.fromMe),
    remoteJid: msg?.key?.remoteJid,
    messageId: msg?.key?.id,
    timestampSec,
    kind: text ? "text" : "other",
    text,
  };
}

async function sendText(route: string, text: string): Promise<string> {
  const jid = routes.jidFor(route);
  if (jid === null) throw new UnresolvedRouteError();
  if (!sock || connection !== "open") throw new Error("not_connected");
  const sent = await sock.sendMessage(jid, { text });
  const id = sent?.key?.id;
  if (typeof id !== "string" || id.length === 0) throw new Error("no_message_id");
  return id;
}

function diagnostics(): Record<string, unknown> {
  return {
    connection_state: connection,
    ready,
    raw_upserts_total: diag.rawUpsertsTotal,
    notify_received_total: diag.notifyReceivedTotal,
    append_ignored_total: diag.appendIgnoredTotal,
    stop_blocked_total: diag.stopBlockedTotal,
    messages_forwarded_total: diag.messagesForwardedTotal,
    handler_errors_total: diag.handlerErrorsTotal,
    last_raw_upsert_at: diag.lastRawUpsertAt,
    last_forward_at: diag.lastForwardAt,
    opened_at: openedAt === null ? null : new Date(openedAt * 1000).toISOString(),
    observe_only: config.observeOnly,
  };
}

async function connect(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(config.authDir);
  connection = "connecting";
  ready = false;
  sock = makeWASocket({
    auth: state,
    browser: ["LaCuisine Bridge", "Desktop", "1.0"],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    logger: pino({ level: "silent" }),
  });
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update: any) => {
    if (update.qr) {
      if (config.observeOnly) {
        // En observación el QR no se dibuja: los registros se guardan en archivo y no deben contener códigos de vinculación.
        log.warn("stage=connection state=qr_required (la sesión no es válida; vincule desde el lanzador normal)");
      } else {
        // QR solo en la terminal local. Nunca se guarda ni se envía.
        qrcode.generate(update.qr, { small: true });
        log.info("QR listo: escanéelo desde WhatsApp > Dispositivos vinculados");
      }
    }
    if (update.connection === "open") {
      connection = "open";
      openedAt = Math.floor(Date.now() / 1000);
      log.info("stage=connection state=open");
      setTimeout(() => {
        // Solo se declara listo si la conexión sigue abierta en este instante (no una apertura anterior).
        if (connection === "open") {
          ready = true;
          log.info("stage=warmup state=closed entrada_habilitada=true");
        }
      }, WARMUP_MS);
    }
    if (update.connection === "close") {
      const code = update.lastDisconnect?.error?.output?.statusCode;
      ready = false;
      if (code === DisconnectReason.loggedOut) {
        connection = "logged_out";
        log.warn("stage=connection state=logged_out (dispositivo desvinculado desde el teléfono; no se reconecta)");
        return;
      }
      connection = "closed";
      log.warn(`stage=connection state=closed code=${code ?? "-"} reintento=5s`);
      setTimeout(() => void connect(), 5_000);
    }
  });

  // Historial y agenda: solo se usan en modo single_chat para proteger contactos existentes.
  const remember = (items: any[] | undefined) => rememberKnownJids(policy, (items ?? []).map((i) => i?.id));
  sock.ev.on("messaging-history.set", (u: any) => {
    remember(u.chats);
    remember(u.contacts);
    log.info(`stage=history_sync isLatest=${Boolean(u.isLatest)}`);
  });
  sock.ev.on("chats.set", (u: any) => remember(u.chats));
  sock.ev.on("contacts.upsert", (items: any[]) => remember(items));

  // Todos los mensajes del arreglo se procesan, no solo el primero. Cualquier excepción queda registrada por nombre.
  sock.ev.on("messages.upsert", async (u: any) => {
    const type = String(u?.type ?? "unknown");
    const messages: any[] = Array.isArray(u?.messages) ? u.messages : [];
    diag.rawUpsertsTotal += 1;
    diag.lastRawUpsertAt = new Date().toISOString();
    log.info(`stage=00_raw_upsert type=${type} count=${messages.length}`);
    try {
      await handleUpsert(type, messages);
    } catch (err) {
      diag.handlerErrorsTotal += 1;
      // Solo el nombre del error: el mensaje puede incluir datos personales.
      log.error(`stage=01_handler_error error=${err instanceof Error ? err.name : "unknown"}`);
    }
  });
}

async function handleUpsert(type: string, messages: any[]): Promise<void> {
  const items: ForwardItem[] = [];
  const nowSec = Math.floor(Date.now() / 1000);
  for (const msg of messages) {
    const messageId = String(msg?.key?.id ?? "");
    const corr = messageId ? cid(messageId) : "-";
    if (type === "notify") diag.notifyReceivedTotal += 1;
    if (isKilled()) {
      // STOP se comprueba antes de clasificar: no consume identificadores de mensaje ni deja rastro de reenvío.
      diag.stopBlockedTotal += 1;
      log.info(`stage=01_classified cid=${corr} decision=ignore reason=stop_active`);
      continue;
    }
    // Sin calentamiento: la sincronización ya se filtra por tipo ('append' nunca se despacha) y por antigüedad.
    const decision = classifyInbound(toRaw(msg, type), policy, nowSec);
    if (decision.action === "ignore") {
      if (decision.reason === "history_sync") diag.appendIgnoredTotal += 1;
      log.info(`stage=01_classified cid=${corr} decision=ignore reason=${decision.reason}`);
      continue;
    }
    log.info(`stage=01_classified cid=${corr} decision=forward`);
    items.push({
      message_id: decision.messageId,
      correlation_id: corr,
      from: decision.routeId,
      timestamp: decision.timestampSec,
      kind: "text",
      text: decision.text,
    });
  }
  if (items.length === 0) return;
  const gate = forwardAllowed(config, nowSec);
  if (!gate.allowed) {
    log.info(`stage=04_forward_skipped reason=${gate.reason} items=${items.length}`);
    return;
  }
  log.info(`stage=02_backend_forward_started items=${items.length}`);
  const result = await forwardToBackend(config.backendUrl, config.secret, items);
  if (result.ok) {
    diag.messagesForwardedTotal += items.length;
    diag.lastForwardAt = new Date().toISOString();
  }
  log.info(`stage=03_backend_forward_${result.ok ? "accepted" : "failed"} http=${result.status} items=${items.length}`);
}

const server = createBridgeServer({
  secret: config.secret,
  isEnabled: () => config.autoReply && inboundWindowOpen(config),
  isKilled,
  inboundUntil: () => config.inboundUntil ?? null,
  connectionState: () => connection,
  diagnostics,
  sendText: (route, text) => sendText(route, text),
  onKill: async () => {
    killedInMemory = true;
    ready = false;
    await sock?.end(undefined);
    log.warn("stage=kill state=closed envios=bloqueados");
    if (config.observeOnly) {
      // En observación, /kill termina el proceso tras contestar la solicitud.
      setTimeout(() => shutdown(0), 300);
    }
  },
  onUnlink: async () => {
    killedInMemory = true;
    await sock?.logout();
    // Las credenciales locales dejan de ser válidas al desvincular: se eliminan del disco.
    rmSync(config.authDir, { recursive: true, force: true });
    log.warn("stage=unlink state=done credenciales_locales=eliminadas cuenta=intacta");
  },
});

server.on("error", (err: NodeJS.ErrnoException) => {
  // Puerto ocupado u otro fallo: se informa y no se continúa con un servidor a medias.
  log.error(`stage=bridge error=${err.code ?? "unknown"}`);
  if (config.observeOnly) releaseLock(observeLock);
  process.exit(1);
});

server.listen(config.port, "127.0.0.1", () => {
  log.info(`stage=bridge listening=127.0.0.1:${config.port} auto_reply=${config.autoReply} modo=${config.inboundMode} observe_only=${config.observeOnly}`);
});

if (config.observeOnly && config.observeUntil !== undefined) {
  // Ventana máxima: al vencer, cierre limpio sin depender de nadie.
  const msLeft = Math.max(0, config.observeUntil * 1000 - Date.now());
  setTimeout(() => {
    log.info("stage=observe state=window_closed");
    shutdown(0);
  }, msLeft);
}

void connect();

process.on("SIGINT", () => shutdown(0));
