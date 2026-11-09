import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createBridgeServer } from "../bridge.js";
import { inboundWindowOpen, loadConfig, PUBLIC_INBOUND_CONFIRMATION, sendAllowed, type WebjsConfig } from "../config.js";
import { sign } from "../hmac.js";
import { newDiag, onIncoming, sendText, type GatewayDeps } from "../gateway.js";
import { BoundedSet, classifyInbound } from "../policy.js";
import { RouteTable } from "../routes.js";
import { UnresolvedRouteError } from "../routes.js";

const NOW = 1_800_000_000;
const SECRET = "secreto-de-prueba-de-al-menos-32-caracteres";
const CLIENT = "573001112233";
const STRANGER = "573009998877";
const LID = "123456789012345@lid";

function baseEnv(over: Record<string, string> = {}): NodeJS.ProcessEnv {
  return {
    WEB_BRIDGE_SECRET: SECRET,
    WEBJS_MODE: "live",
    WEB_BRIDGE_AUTO_REPLY: "true",
    WEB_BRIDGE_INBOUND_MODE: "public_inbound",
    WEB_BRIDGE_PUBLIC_CONFIRMATION: PUBLIC_INBOUND_CONFIRMATION,
    ...over,
  };
}

// ---------- Configuración: activación explícita, sin ventana ----------

test("atención pública: exige auto-respuesta y la confirmación exacta", () => {
  assert.throws(() => loadConfig(baseEnv({ WEB_BRIDGE_AUTO_REPLY: "false" }), NOW), /requiere WEB_BRIDGE_AUTO_REPLY/);
  assert.throws(() => loadConfig(baseEnv({ WEB_BRIDGE_PUBLIC_CONFIRMATION: "si" }), NOW), /WEB_BRIDGE_PUBLIC_CONFIRMATION exacta/);
  assert.throws(() => loadConfig(baseEnv({ WEB_BRIDGE_PUBLIC_CONFIRMATION: "" }), NOW), /WEB_BRIDGE_PUBLIC_CONFIRMATION exacta/);
});

test("atención pública: no admite ventana temporal y queda abierta sin caducidad", () => {
  assert.throws(() => loadConfig(baseEnv({ WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 60) }), NOW), /sin ventana/);
  const cfg = loadConfig(baseEnv(), NOW);
  assert.equal(cfg.inboundMode, "public_inbound");
  assert.equal(inboundWindowOpen(cfg, NOW + 10 * 365 * 86400), true);
  assert.equal(sendAllowed(cfg, NOW), true);
});

test("sin confirmación, el modo de entrada pública no existe: el valor por defecto sigue siendo single_chat", () => {
  const cfg = loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "live" }, NOW);
  assert.equal(cfg.inboundMode, "single_chat");
});

// ---------- Clasificación: sin lista, sin agenda, pero con filtros intactos ----------

const pubState = (publicInbound: boolean) => ({
  seen: new BoundedSet(100),
  routeFor: (chatId: string) => (chatId.endsWith("@lid") ? "912345678901" : chatId.slice(0, -5)),
  publicInbound,
});

function live(over: Record<string, unknown> = {}) {
  return {
    fromMe: false,
    from: `${CLIENT}@c.us`,
    type: "chat",
    body: "Hola, ¿qué cafeteras tienen?",
    id: "false_x@c.us_ID1",
    timestampSec: NOW - 5,
    broadcast: false,
    isStatus: false,
    ...over,
  };
}

test("público: un número nuevo que escribe se acepta sin estar en ninguna lista", () => {
  const d = classifyInbound(live({ from: `${STRANGER}@c.us`, id: "false_s@c.us_ID9" }), pubState(true), NOW);
  assert.equal(d.action, "forward");
});

test("público: un @lid sin número verificado se acepta; la identidad queda nula, no inventada", () => {
  const d = classifyInbound(live({ from: LID, id: "false_l_ID2" }), pubState(true), NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") assert.equal(d.identity, null);
});

test("público: los filtros de grupos, propios, estados e historial siguen intactos", () => {
  assert.equal((classifyInbound(live({ fromMe: true }), pubState(true), NOW) as any).reason, "own_message");
  assert.equal((classifyInbound(live({ from: "120363000000000000@g.us" }), pubState(true), NOW) as any).reason, "group");
  assert.equal((classifyInbound(live({ from: "status@broadcast" }), pubState(true), NOW) as any).reason, "status_or_broadcast");
  assert.equal((classifyInbound(live({ timestampSec: NOW - 900 }), pubState(true), NOW) as any).reason, "too_old");
});

test("sin modo público, un @lid sin identidad verificada sigue descartándose", () => {
  assert.equal((classifyInbound(live({ from: LID }), pubState(false), NOW) as any).reason, "identity_unverified");
});

// ---------- Envío: solo a rutas nacidas de un evento entrante reenviado ----------

function harness(cfgOver: Partial<WebjsConfig> = {}) {
  const dir = mkdtempSync(join(tmpdir(), "pub-"));
  const routes = new RouteTable(Buffer.alloc(32, 5), join(dir, "r.json"), join(dir, "i.json"), join(dir, "k.json"));
  const config: WebjsConfig = {
    secret: SECRET, backendUrl: "http://127.0.0.1:8000", port: 8787, mode: "live", autoReply: true,
    inboundMode: "public_inbound", readyTimeoutSec: 120, authDir: join(dir, "a"), stateDir: dir, logDir: dir,
    ...cfgOver,
  };
  const sent: Array<[string, string]> = [];
  const forwarded: Array<{ from: string; identity: string | null }> = [];
  const deps: GatewayDeps = {
    config,
    routes,
    policy: { seen: new BoundedSet(100), routeFor: (c) => routes.routeFor(c), publicInbound: true },
    diag: newDiag(),
    state: () => "ready",
    isKilled: () => false,
    nowSec: () => NOW,
    transport: { sendMessage: async (chatId, text) => { sent.push([chatId, text]); return { id: { _serialized: "wid" + sent.length } }; } },
    forward: async (items) => {
      forwarded.push(...items.map((i) => ({ from: i.from, identity: i.identity })));
      // Como el backend real: confirma cada elemento como admitido.
      return { ok: true, status: 200, items: items.map((i) => ({ correlation_id: i.correlation_id, outcome: "accepted", reason: null })) };
    },
    log: () => undefined,
  };
  return { deps, routes, sent, forwarded };
}

test("público: una ruta desconocida no recibe nada, aunque el worker la pida", async () => {
  const h = harness();
  await assert.rejects(sendText("573999999999", "hola", h.deps), (e: unknown) => e instanceof UnresolvedRouteError);
  assert.equal(h.sent.length, 0);
});

test("público: un cliente nuevo recibe la respuesta en su mismo chat tras su propio mensaje", async () => {
  const h = harness();
  await onIncoming({ fromMe: false, from: LID, type: "chat", body: "Hola", id: "false_lx_1", timestampSec: NOW - 5 }, h.deps);
  assert.equal(h.forwarded.length, 1);
  const route = h.forwarded[0]!.from;
  assert.ok(h.routes.knownRoute(route));
  await sendText(route, "Respuesta comercial", h.deps);
  assert.deepEqual(h.sent, [[LID, "Respuesta comercial"]]);
});

test("público: el número de un @c.us que escribió queda conocido y no el de otro", async () => {
  const h = harness();
  await onIncoming({ fromMe: false, from: `${CLIENT}@c.us`, type: "chat", body: "Hola", id: "false_c_1", timestampSec: NOW - 5 }, h.deps);
  await sendText(CLIENT, "hola cliente", h.deps);
  assert.deepEqual(h.sent, [[`${CLIENT}@c.us`, "hola cliente"]]);
  await assert.rejects(sendText(STRANGER, "intruso", h.deps), UnresolvedRouteError);
});

test("público: el conocimiento de rutas sobrevive a un reinicio y no admite rutas inventadas", () => {
  const dir = mkdtempSync(join(tmpdir(), "pub-"));
  const first = new RouteTable(Buffer.alloc(32, 6), join(dir, "r.json"), join(dir, "i.json"), join(dir, "k.json"));
  first.markKnown(CLIENT);
  const again = new RouteTable(Buffer.alloc(32, 6), join(dir, "r.json"), join(dir, "i.json"), join(dir, "k.json"));
  assert.equal(again.knownRoute(CLIENT), true);
  assert.equal(again.knownRoute(STRANGER), false);
  again.markKnown("no-es-ruta");
  assert.equal(again.knownRoute("no-es-ruta"), false);
});

// ---------- Endpoint firmado /route ----------

function startBridge(routeKnown: (r: string) => boolean) {
  const server = createBridgeServer({
    secret: SECRET, isEnabled: () => true, isKilled: () => false, connectionState: () => "ready",
    routeKnown, sendText: async () => "x", onKill: async () => undefined,
  });
  return new Promise<{ url: string; close: () => void }>((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const port = (server.address() as { port: number }).port;
      resolve({ url: `http://127.0.0.1:${port}`, close: () => server.close() });
    });
  });
}

function postRoute(url: string, body: string, signature?: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const stamp = String(Math.floor(Date.now() / 1000));
    const req = http.request(`${url}/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
        "X-Bridge-Signature": signature ?? sign(SECRET, "status", stamp, body) },
    }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

test("puente: /route responde known solo con rutas reenviadas y exige firma", async () => {
  const bridge = await startBridge((r) => r === CLIENT);
  try {
    const yes = await postRoute(bridge.url, JSON.stringify({ route: CLIENT }));
    assert.deepEqual(JSON.parse(yes.body), { known: true });
    const no = await postRoute(bridge.url, JSON.stringify({ route: STRANGER }));
    assert.deepEqual(JSON.parse(no.body), { known: false });
    const unsigned = await postRoute(bridge.url, JSON.stringify({ route: CLIENT }), "00");
    assert.equal(unsigned.status, 401);
  } finally {
    bridge.close();
  }
});

test("continuidad: cinco intercambios del mismo cliente y otro cliente intercalado, sin duplicados", async () => {
  const h = harness();
  const msg = (from: string, n: number, body = "Mensaje " + n) => ({
    fromMe: false, from, type: "chat", body, id: `false_${from}_M${n}_${from.slice(-3)}`, timestampSec: NOW - 5,
  });
  for (let n = 1; n <= 5; n++) {
    await onIncoming(msg(`${CLIENT}@c.us`, n), h.deps);
    await onIncoming(msg(`${STRANGER}@c.us`, n), h.deps);
  }
  assert.equal(h.forwarded.length, 10, "cada mensaje nuevo produce exactamente un reenvío");
  const clientRoutes = new Set(h.forwarded.filter((f) => f.from === CLIENT).map((f) => f.from));
  assert.deepEqual([...clientRoutes], [CLIENT], "el cliente conserva una sola ruta de respuesta");
  // Un duplicado exacto del mismo mensaje no genera nueva respuesta.
  await onIncoming(msg(`${CLIENT}@c.us`, 3), h.deps);
  assert.equal(h.forwarded.length, 10);
  // Cada cliente recibe su respuesta en su propio chat.
  await sendText(CLIENT, "respuesta a cliente 1", h.deps);
  await sendText(STRANGER, "respuesta a otro cliente", h.deps);
  assert.deepEqual(h.sent, [[`${CLIENT}@c.us`, "respuesta a cliente 1"], [`${STRANGER}@c.us`, "respuesta a otro cliente"]]);
});

test("un solo chat con WEB_BRIDGE_INBOUND_UNTIL: solo fija el cierre del proceso, sin ventana de aceptación", () => {
  const cfg = loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "live", WEB_BRIDGE_AUTO_REPLY: "true", WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 300) }, NOW);
  assert.equal(cfg.inboundMode, "single_chat");
  assert.equal(cfg.inboundUntil, NOW + 300);
  assert.equal(inboundWindowOpen(cfg, NOW + 10_000), true, "la aceptación de un solo chat no depende de la ventana");
  assert.throws(() => loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "live", WEB_BRIDGE_AUTO_REPLY: "true", WEB_BRIDGE_INBOUND_UNTIL: String(NOW - 5) }, NOW), /WEB_BRIDGE_INBOUND_UNTIL/);
  assert.throws(() => loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "live", WEB_BRIDGE_AUTO_REPLY: "true", WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 4000) }, NOW), /WEB_BRIDGE_INBOUND_UNTIL/);
});
