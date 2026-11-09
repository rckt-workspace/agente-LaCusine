import { strict as assert } from "node:assert";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { loadConfig, type WebjsConfig } from "../config.js";
import type { ForwardItem, ForwardResult } from "../forwarder.js";
import { newDiag, onIncoming, sendText, type GatewayDeps, type ConnState } from "../gateway.js";
import { BoundedSet, type InboundFacts } from "../policy.js";
import { RouteTable, UnresolvedRouteError } from "../routes.js";

const NOW = 1_800_000_000;
const SECRET = "s3cr3t-de-prueba-de-32-caracteres!!";
const CLIENT = "573001112233@c.us";
const LID = "123456789012345@lid";

interface Harness {
  deps: GatewayDeps;
  forwards: ForwardItem[][];
  sends: Array<[string, string]>;
  logs: string[];
  setState(s: ConnState): void;
  setForwardResult(r: ForwardResult): void;
  setSendBehavior(b: "ok" | "throw" | "no_id"): void;
}

function harness(over: Partial<WebjsConfig> = {}, dir = mkdtempSync(join(tmpdir(), "gw-"))): Harness {
  const config: WebjsConfig = {
    secret: SECRET,
    backendUrl: "http://127.0.0.1:8000",
    port: 8787,
    mode: "live",
    autoReply: true,
    inboundMode: "single_chat",
    readyTimeoutSec: 120,
    authDir: join(dir, "auth"),
    stateDir: dir,
    logDir: dir,
    ...over,
  };
  const routes = new RouteTable(Buffer.alloc(32, 7), join(dir, "routes.json"));
  let state: ConnState = "ready";
  let forwardResult: ForwardResult = { ok: true, status: 200 };
  let sendBehavior: "ok" | "throw" | "no_id" = "ok";
  let sendCount = 0;
  const forwards: ForwardItem[][] = [];
  const sends: Array<[string, string]> = [];
  const logs: string[] = [];
  const deps: GatewayDeps = {
    config,
    routes,
    policy: { seen: new BoundedSet(100), routeFor: (chatId) => routes.routeFor(chatId) },
    diag: newDiag(),
    state: () => state,
    isKilled: () => false,
    nowSec: () => NOW,
    transport: {
      sendMessage: async (chatId, text) => {
        sendCount += 1;
        sends.push([chatId, text]);
        if (sendBehavior === "throw") throw new Error("socket_reset");
        if (sendBehavior === "no_id") return { id: {} };
        return { id: { _serialized: `true_${chatId}_WEBJS${sendCount}` } };
      },
    },
    forward: async (items) => {
      forwards.push(items);
      // Como el backend real: un 200 confirma el resultado de cada elemento.
      if (forwardResult.ok && !forwardResult.items) {
        return { ...forwardResult, items: items.map((i) => ({ correlation_id: i.correlation_id, outcome: "accepted", reason: null })) };
      }
      return forwardResult;
    },
    log: (line) => logs.push(line),
  };
  return {
    deps,
    forwards,
    sends,
    logs,
    setState: (s) => (state = s),
    setForwardResult: (r) => (forwardResult = r),
    setSendBehavior: (b) => (sendBehavior = b),
  };
}

function incoming(over: Partial<InboundFacts> = {}): InboundFacts {
  return {
    fromMe: false,
    from: CLIENT,
    type: "chat",
    body: "Quiero una cotización",
    id: "MSG-1",
    timestampSec: NOW - 3,
    broadcast: false,
    isStatus: false,
    ...over,
  };
}

test("recepción: un mensaje nuevo llega al backend una sola vez con la forma esperada", async () => {
  const h = harness();
  await onIncoming(incoming(), h.deps);
  await onIncoming(incoming(), h.deps); // mismo evento repetido
  assert.equal(h.forwards.length, 1);
  const [item] = h.forwards[0]!;
  assert.equal(item!.from, "573001112233");
  assert.equal(item!.kind, "text");
  assert.equal(item!.text, "Quiero una cotización");
  assert.equal(h.deps.diag.messagesForwardedTotal, 1);
  assert.equal(h.deps.diag.ignoredTotal, 1);
});

test("recepción: los registros no contienen texto, número completo ni identificador de mensaje", async () => {
  const h = harness();
  await onIncoming(incoming({ id: "SECRETID-42" }), h.deps);
  const all = h.logs.join("\n");
  assert.ok(!all.includes("Quiero una cotización"));
  assert.ok(!all.includes("573001112233"));
  assert.ok(!all.includes("SECRETID-42"));
  assert.ok(all.includes("stage=00_message_received"));
  assert.ok(all.includes("stage=01_classified"));
  assert.ok(all.includes("stage=02_backend_forward"));
  assert.ok(all.includes("stage=03_backend_forward_accepted"));
});

test("recepción: con STOP activo nada llega al backend y queda contado", async () => {
  const h = harness();
  h.deps.isKilled = () => true;
  await onIncoming(incoming(), h.deps);
  assert.equal(h.forwards.length, 0);
  assert.equal(h.deps.diag.stopBlockedTotal, 1);
  assert.ok(h.logs.some((l) => l.includes("reason=stop_active")));
});

test("recepción: el modo observación clasifica pero nunca reenvía", async () => {
  const h = harness({ mode: "observe", autoReply: false, observeUntil: NOW + 600 });
  await onIncoming(incoming(), h.deps);
  assert.equal(h.forwards.length, 0);
  assert.ok(h.logs.some((l) => l.includes("stage=04_forward_skipped reason=observe_only")));
});

test("recepción: sin respuestas automáticas el mensaje no sale del conector", async () => {
  const h = harness({ autoReply: false });
  await onIncoming(incoming(), h.deps);
  assert.equal(h.forwards.length, 0);
  assert.ok(h.logs.some((l) => l.includes("reason=auto_reply_off")));
});

test("recepción: un fallo del backend se cuenta y no reintenta automáticamente", async () => {
  const h = harness();
  h.setForwardResult({ ok: false, status: 500 });
  await onIncoming(incoming(), h.deps);
  assert.equal(h.forwards.length, 1);
  assert.equal(h.deps.diag.messagesForwardedTotal, 0);
  assert.ok(h.logs.some((l) => l.includes("stage=03_backend_forward_failed http=500")));
});

test("recepción: un mensaje @lid se reenvía con identificador opaco y su identidad verificada", async () => {
  const h = harness();
  await onIncoming(incoming({ from: LID, id: "LID-1", identity: "573153863179" }), h.deps);
  assert.equal(h.forwards.length, 1);
  const item = h.forwards[0]![0]!;
  assert.match(item.from, /^9[0-9]{11}$/);
  assert.ok(!LID.includes(item.from));
  assert.equal(item.identity, "573153863179");
});

test("recepción: un @lid sin identidad verificada no sale al backend", async () => {
  const h = harness();
  await onIncoming(incoming({ from: LID, id: "LID-3" }), h.deps);
  assert.equal(h.forwards.length, 0);
  assert.ok(h.logs.some((l) => l.includes("reason=identity_unverified")));
});

test("envío: la respuesta va exactamente al mismo chat de origen, incluido @lid", async () => {
  const h = harness();
  await onIncoming(incoming({ from: LID, id: "LID-2", identity: "573153863179" }), h.deps);
  const route = h.forwards[0]![0]!.from;
  const id = await sendText(route, "Hola, le respondemos", h.deps);
  assert.deepEqual(h.sends, [[LID, "Hola, le respondemos"]]);
  assert.match(id, /WEBJS/);
  assert.equal(h.deps.diag.sendsTotal, 1);
});

test("envío: un chat individual responde a digitos@c.us", async () => {
  const h = harness();
  await onIncoming(incoming(), h.deps);
  await sendText("573001112233", "hola", h.deps);
  assert.deepEqual(h.sends, [[CLIENT, "hola"]]);
});

test("envío: sin sesión lista no sale nada y se rechaza como no enviado", async () => {
  const h = harness();
  h.setState("disconnected");
  await assert.rejects(sendText("573001112233", "hola", h.deps), /not_connected/);
  assert.equal(h.sends.length, 0);
});

test("envío: con respuestas desactivadas o en observación nada sale", async () => {
  const off = harness({ autoReply: false });
  await assert.rejects(sendText("573001112233", "hola", off.deps), /send_disabled/);
  const obs = harness({ mode: "observe", autoReply: false, observeUntil: NOW + 600 });
  await assert.rejects(sendText("573001112233", "hola", obs.deps), /send_disabled/);
  assert.equal(off.sends.length + obs.sends.length, 0);
});

test("envío: una dirección no resoluble bloquea el envío", async () => {
  const h = harness();
  await assert.rejects(sendText("9123", "hola", h.deps), UnresolvedRouteError);
  await assert.rejects(sendText("no-es-un-destino", "hola", h.deps), UnresolvedRouteError);
  assert.equal(h.sends.length, 0);
});

test("envío: un error del transporte se propaga como resultado incierto y no se reintenta aquí", async () => {
  const h = harness();
  await onIncoming(incoming({ from: LID, id: "LID-3", identity: "573153863179" }), h.deps);
  const route = h.forwards[0]![0]!.from;
  h.setSendBehavior("throw");
  await assert.rejects(sendText(route, "hola", h.deps), /socket_reset/);
  assert.equal(h.sends.length, 1, "exactamente un intento");
  assert.equal(h.deps.diag.sendFailuresTotal, 1);
});

test("envío: si WhatsApp no devuelve identificador el resultado es incierto", async () => {
  const h = harness();
  h.setSendBehavior("no_id");
  await assert.rejects(sendText("573001112233", "hola", h.deps), /no_message_id/);
  assert.equal(h.deps.diag.sendsTotal, 0);
});

test("configuración: observación sin respuestas y con ventana obligatoria", () => {
  assert.throws(
    () => loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "observe", WEB_BRIDGE_AUTO_REPLY: "true",
      WEBJS_OBSERVE_UNTIL: String(NOW + 60) }, NOW),
    /observe no admite/,
  );
  assert.throws(() => loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "observe" }, NOW), /WEBJS_OBSERVE_UNTIL/);
});

test("dependencia: whatsapp-web.js está fijada a una versión exacta, sin rangos", () => {
  const pkg = JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "package.json"), "utf8")) as {
    dependencies: Record<string, string>;
  };
  assert.match(pkg.dependencies["whatsapp-web.js"] ?? "", /^\d+\.\d+\.\d+$/);
});
