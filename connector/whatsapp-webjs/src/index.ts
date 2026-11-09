// Conector WhatsApp Web con whatsapp-web.js (dispositivo vinculado). EXPERIMENTAL y NO oficial.
//
// Garantías:
//   * Un único conector por sesión (cerrojo en state/webjs.lock).
//   * El modo observe nunca reenvía al backend ni envía. El modo live reenvía y envía solo con
//     WEB_BRIDGE_AUTO_REPLY=true y dentro de la ventana autorizada.
//   * STOP (state/STOP) bloquea reenvío y envío sin cerrar la sesión.
//   * Los registros no contienen texto, números completos, nombres ni códigos QR.
//   * El cierre (SIGINT, /kill, fin de ventana) destruye el navegador SIN desvincular el dispositivo.
//   * Sin reconexión automática: una sesión caída queda marcada y el envío falla cerrado.
//   * "authenticated" no basta: si "ready" no llega dentro del plazo, el conector termina con error (código 2)
//     y nunca declara la sesión operativa.
//   * Todos los listeners de whatsapp-web.js se registran ANTES de client.initialize().
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createLogger, say } from "./logging.js";
import { printQr } from "./qr.js";
import wwebjs from "whatsapp-web.js";
import { createBridgeServer } from "./bridge.js";
import { deadlineOf, loadConfig, sendAllowed, forwardAllowed } from "./config.js";
import { errorSummary, pageProbe, sanitize, wwebjsVersion } from "./diagnostics.js";
import { forwardToBackend } from "./forwarder.js";
import { fieldsLine, missingRequirements, normalizeMessage } from "./inbound.js";
import { verifyLidIdentity } from "./identity.js";
import { acquireLock, releaseLock } from "./lock.js";
import { BoundedSet, type InboundFacts } from "./policy.js";
import { newDiag, onIncoming, sendText, type ConnState, type GatewayDeps } from "./gateway.js";
import { ReadyGate } from "./ready.js";
import { routeTableFor } from "./routes.js";

const { Client, LocalAuth } = wwebjs;

const config = loadConfig();
mkdirSync(config.stateDir, { recursive: true });
mkdirSync(config.logDir, { recursive: true });
const stopFile = join(config.stateDir, "STOP");
const lockFile = join(config.stateDir, "webjs.lock");
const sentFile = join(config.stateDir, "sent-webjs.json");
const logFile = process.env.WEBJS_LOG_FILE ?? join(config.logDir, "connector-webjs.log");

// Registro en archivo: único escritor de este proceso. El QR nunca pasa por aquí.
const log = createLogger(logFile);

const lock = acquireLock(lockFile);
if (!lock.ok) {
  log.error("stage=startup refused reason=lock_held (otro conector usa esta sesión)");
  process.exit(1);
}

let connState: ConnState = "starting";
let killedInMemory = false;
let sock: any = null;
const isKilled = (): boolean => killedInMemory || existsSync(stopFile);
const nowSec = (): number => Math.floor(Date.now() / 1000);

// Idempotencia persistente: clave de envío -> identificador de mensaje. Acotada a las últimas 2000.
function loadSent(): Record<string, string> {
  try {
    return JSON.parse(readFileSync(sentFile, "utf8")) as Record<string, string>;
  } catch {
    return {};
  }
}
const sentKeys = loadSent();
const sentStore = {
  get: (key: string) => sentKeys[key],
  set: (key: string, messageId: string) => {
    sentKeys[key] = messageId;
    const keys = Object.keys(sentKeys);
    for (const old of keys.slice(0, Math.max(0, keys.length - 2000))) delete sentKeys[old];
    writeFileSync(sentFile, JSON.stringify(sentKeys), { encoding: "utf8", mode: 0o600 });
  },
};

const routes = routeTableFor(config.stateDir);
const diag = newDiag();
// Contadores operativos de arranque y navegador (sin contenido ni identificadores).
const ops = { pageErrors: 0, pageConsoleErrors: 0, browserDisconnects: 0, initFailures: 0 };
// Fin de la ventana útil. En observación con duración relativa se fija al confirmarse "ready".
let deadline: number | undefined = deadlineOf(config);

const deps: GatewayDeps = {
  config,
  routes,
  policy: {
    seen: new BoundedSet(5000),
    routeFor: (chatId) => routes.routeFor(chatId),
    publicInbound: config.inboundMode === "public_inbound",
  },
  diag,
  state: () => connState,
  isKilled,
  nowSec,
  transport: {
    sendMessage: (chatId, text) => client.sendMessage(chatId, text),
    // Solo lectura de la resolución del chat en WhatsApp Web (la misma que usa sendMessage).
    chatResolved: async (chatId) =>
      Boolean(
        await (client as any).pupPage.evaluate(
          async (id: string) => Boolean(await (window as any).WWebJS.getChat(id, { getAsModel: false })),
          chatId,
        ),
      ),
  },
  forward: (items) => forwardToBackend(config.backendUrl, config.secret, items),
  log: (line) => log.info(line),
};

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: config.authDir }),
  puppeteer: {
    headless: true,
    executablePath: config.chromePath,
    args: ["--no-first-run", "--no-default-browser-check", "--disable-gpu"],
  },
});

// Cierre limpio: destruye el navegador. No llama a logout(), así que el dispositivo sigue vinculado.
let closing = false;
function shutdown(code: number): void {
  if (closing) return;
  closing = true;
  clearWatchdog();
  const finish = () => {
    releaseLock(lockFile);
    server.close();
    process.exit(code);
  };
  const fallback = setTimeout(finish, 5_000);
  client
    .destroy()
    .catch(() => undefined)
    .finally(() => {
      clearTimeout(fallback);
      finish();
    });
}

function setState(next: ConnState): void {
  connState = next;
}

// ---------- Vigilancia de "ready" ----------
const readyGate = new ReadyGate(config.readyTimeoutSec * 1000);
// La inyección de whatsapp-web.js espera hasta 30 s por sus módulos: la sonda se toma después de esa espera.
const PROBE_AFTER_MS = Math.min(35_000, Math.floor((config.readyTimeoutSec * 1000) / 2));
let probeLogged = false;
let watchdog: NodeJS.Timeout | null = null;

function clearWatchdog(): void {
  if (watchdog) clearInterval(watchdog);
  watchdog = null;
}

// Sonda de solo lectura con plazo propio: nunca bloquea el cierre.
async function logProbe(stage: string): Promise<void> {
  const page = (client as any).pupPage;
  if (!page) {
    log.warn(`stage=${stage} page=absent`);
    return;
  }
  const timeout = new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), 5_000));
  try {
    const result = await Promise.race([page.evaluate(pageProbe), timeout]);
    log.warn(`stage=${stage} probe=${typeof result === "string" ? result : JSON.stringify(result)}`);
  } catch (err) {
    log.warn(`stage=${stage} probe=error ${errorSummary(err)}`);
  }
}

function startReadyWatchdog(): void {
  if (watchdog) return;
  watchdog = setInterval(() => {
    const now = Date.now();
    const phase = readyGate.check(now);
    if (phase === "waiting_ready" && !probeLogged && readyGate.elapsedMs(now) >= PROBE_AFTER_MS) {
      probeLogged = true;
      log.warn(`stage=ready state=waiting elapsed_s=${Math.round(readyGate.elapsedMs(now) / 1000)}`);
      void logProbe("ready_probe");
    }
    if (phase === "timed_out") {
      clearWatchdog();
      log.error(`stage=ready state=timeout limit_s=${config.readyTimeoutSec} (authenticated sin ready; no se declara listo)`);
      say(`WhatsApp no confirmó la sesión en ${config.readyTimeoutSec} s. El conector se cierra. Nada se ha enviado.`);
      void logProbe("ready_timeout_probe").finally(() => shutdown(2));
    }
  }, 1000);
}

// ---------- Eventos de whatsapp-web.js (todos antes de initialize) ----------
client.on("qr", (qr: string) => {
  setState("qr_required");
  diag.qrEventsTotal += 1;
  log.info("stage=connection state=qr_required");
  if (config.mode === "live") {
    // Solo en consola (UTF-8): el código de vinculación no se guarda en ningún registro.
    void printQr(qr, (text) => process.stdout.write(text));
  }
});

client.on("authenticated", () => {
  setState("authenticated");
  log.info("stage=connection state=authenticated");
  readyGate.authenticated(Date.now());
  startReadyWatchdog();
  say(`Vinculación aceptada. Esperando confirmación de la sesión (plazo ${config.readyTimeoutSec} s)...`);
});

client.on("ready", async () => {
  if (readyGate.current === "timed_out") return; // el plazo ya venció: el cierre está en curso
  // "ready" no basta: se confirma el estado real de la sesión antes de declararla operativa.
  let real: string | null = null;
  try {
    real = await client.getState();
  } catch {
    real = null;
  }
  if (real === "CONNECTED") {
    readyGate.ready();
    clearWatchdog();
    setState("ready");
    log.info("stage=connection state=ready");
    say("WhatsApp conectado y listo. Puede cerrar esta ventana solo con -Detener.");
    startObserveWindowIfRelative();
  } else {
    log.warn(`stage=connection state=not_confirmed wa_state=${sanitize(real ?? "unknown", 40)}`);
  }
});

client.on("auth_failure", () => {
  setState("auth_failure");
  diag.authFailuresTotal += 1;
  // No se borra la sesión: un fallo de autenticación se resuelve revinculando, no borrando archivos.
  log.error("stage=connection state=auth_failure (sesión no válida; no se borra; revincular)");
  say("La sesión no es válida. No se borra nada: use -Vincular para revincular.");
});

client.on("disconnected", (reason: string) => {
  const logout = reason === "LOGOUT";
  setState(logout ? "logged_out" : "disconnected");
  diag.disconnectsTotal += 1;
  log.warn(`stage=connection state=${connState} reason=${logout ? "LOGOUT" : "other"}`);
});

client.on("loading_screen", (percent: number) => {
  log.info(`stage=loading percent=${Number(percent)}`);
});

client.on("change_state", (state: string) => {
  log.info(`stage=connection wa_state=${sanitize(state, 40)}`);
});

client.on("message", async (msg: any) => {
  let facts: InboundFacts;
  let fields: { line: string; missing: string[] };
  try {
    const normalized = normalizeMessage(msg);
    facts = normalized.facts;
    let identityCheck = "not_needed";
    if (facts.from?.endsWith("@lid")) {
      // El número se toma solo del mapeo de WhatsApp, nunca del mensaje ni de la agenda.
      const outcome = await verifyLidIdentity(facts.from, (ids) => (client as any).getContactLidAndPhone(ids));
      if (outcome.ok) {
        const route = routes.routeFor(facts.from);
        if (route !== null && routes.recordIdentity(route, outcome.phone)) {
          facts.identity = outcome.phone;
          identityCheck = "verified";
        } else {
          identityCheck = "identity_conflict";
        }
      } else {
        identityCheck = outcome.reason;
      }
    }
    fields = {
      line: `${fieldsLine(normalized.indicators)} identity_check=${identityCheck}`,
      missing: missingRequirements(normalized.indicators),
    };
  } catch {
    diag.handlerErrorsTotal += 1;
    log.error("stage=01_handler_error error=normalize");
    return;
  }
  try {
    await onIncoming(facts, deps, fields);
  } catch (err) {
    diag.handlerErrorsTotal += 1;
    // Solo el nombre del error: el mensaje puede incluir datos personales.
    log.error(`stage=01_handler_error error=${err instanceof Error ? err.name : "unknown"}`);
  }
});

// ---------- Navegador y página (Puppeteer): se enganchan en cuanto existen ----------
function instrumentBrowserAndPage(): void {
  let browserHooked = false;
  let pageHooked = false;
  const timer = setInterval(() => {
    const browser = (client as any).pupBrowser;
    const page = (client as any).pupPage;
    if (browser && !browserHooked) {
      browserHooked = true;
      browser.on("disconnected", () => {
        if (closing) return; // cierre propio: no es un fallo
        ops.browserDisconnects += 1;
        setState("disconnected");
        log.error("stage=browser state=disconnected_unexpectedly");
      });
    }
    if (page && !pageHooked) {
      pageHooked = true;
      page.on("pageerror", (err: unknown) => {
        ops.pageErrors += 1;
        log.error(`stage=page error=pageerror ${errorSummary(err)}`);
      });
      page.on("error", (err: unknown) => {
        ops.pageErrors += 1;
        log.error(`stage=page error=crash ${errorSummary(err)}`);
      });
      page.on("console", (msg: any) => {
        // Solo se cuenta: el texto de la consola de WhatsApp Web puede contener datos personales.
        if (msg?.type?.() === "error") ops.pageConsoleErrors += 1;
      });
    }
    if (browserHooked && pageHooked) clearInterval(timer);
  }, 250);
}

// ---------- Ventana de observación relativa a "ready" ----------
function startObserveWindowIfRelative(): void {
  if (config.mode !== "observe" || config.observeSeconds === undefined || deadline !== undefined) return;
  deadline = nowSec() + config.observeSeconds;
  log.info(`stage=window state=started seconds=${config.observeSeconds}`);
  setTimeout(() => {
    log.info("stage=window state=closed");
    shutdown(0);
  }, config.observeSeconds * 1000);
}

function diagnostics(): Record<string, unknown> {
  return {
    mode: config.mode,
    observe_only: config.mode === "observe",
    connection_state: connState,
    ready: connState === "ready",
    ready_phase: readyGate.current,
    ready_timeout_seconds: config.readyTimeoutSec,
    message_events_total: diag.rawEventsTotal,
    ignored_total: diag.ignoredTotal,
    stop_blocked_total: diag.stopBlockedTotal,
    messages_forwarded_total: diag.messagesForwardedTotal,
    handler_errors_total: diag.handlerErrorsTotal,
    sends_total: diag.sendsTotal,
    send_failures_total: diag.sendFailuresTotal,
    auth_failures_total: diag.authFailuresTotal,
    disconnects_total: diag.disconnectsTotal,
    qr_events_total: diag.qrEventsTotal,
    page_errors_total: ops.pageErrors,
    page_console_errors_total: ops.pageConsoleErrors,
    browser_disconnects_total: ops.browserDisconnects,
    init_failures_total: ops.initFailures,
    last_message_event_at: diag.lastRawAt,
    last_forward_at: diag.lastForwardAt,
  };
}

const server = createBridgeServer({
  secret: config.secret,
  isEnabled: () => sendAllowed(config),
  isKilled,
  inboundUntil: () => deadline ?? null,
  // Contrato con el backend: "open" solo cuando la sesión está confirmada como operativa.
  connectionState: () => (connState === "ready" ? "open" : connState),
  diagnostics,
  identityOf: (route) => routes.identityFor(route),
  routeKnown: (route) => routes.knownRoute(route),
  sendText: async (route, text) => {
    try {
      return await sendText(route, text, deps);
    } catch (err) {
      // Solo tipo y mensaje saneado: el puente responde 500 sin detalle, y aquí queda la causa para diagnóstico.
      log.error(`stage=05_send_failed ${errorSummary(err)}`);
      throw err;
    }
  },
  idempotency: sentStore,
  onKill: async () => {
    killedInMemory = true;
    log.warn("stage=kill state=requested envios=bloqueados");
    if (config.mode === "observe") setTimeout(() => shutdown(0), 300);
  },
});

server.on("error", (err: NodeJS.ErrnoException) => {
  log.error(`stage=bridge error=${err.code ?? "unknown"}`);
  releaseLock(lockFile);
  process.exit(1);
});

server.listen(config.port, "127.0.0.1", () => {
  log.info(`stage=bridge listening=127.0.0.1:${config.port} mode=${config.mode} auto_reply=${config.autoReply} inbound=${config.inboundMode}`);
});

if (deadline !== undefined) {
  // Ventana máxima absoluta (live, o observación con WEBJS_OBSERVE_UNTIL): cierre limpio al vencer.
  setTimeout(() => {
    log.info("stage=window state=closed");
    shutdown(0);
  }, Math.max(0, deadline * 1000 - Date.now()));
}

process.on("unhandledRejection", (reason) => {
  // Falla cerrado: se registra el tipo de error y se cierra con el navegador destruido.
  log.error(`stage=process error=unhandled_rejection name=${reason instanceof Error ? reason.name : "unknown"}`);
  shutdown(1);
});
process.on("SIGINT", () => shutdown(0));

log.info(
  `stage=startup mode=${config.mode} lib=${wwebjsVersion()} ready_limit_s=${config.readyTimeoutSec} ` +
    `send_allowed=${sendAllowed(config)} forward=${JSON.stringify(forwardAllowed(config))}`,
);
instrumentBrowserAndPage();
client.initialize().catch((err: unknown) => {
  setState("disconnected");
  ops.initFailures += 1;
  log.error(`stage=init state=failed ${errorSummary(err)}`);
  say("No se pudo iniciar WhatsApp Web en el navegador. Revise el registro. Nada se ha enviado.");
  shutdown(2);
});
