import { strict as assert } from "node:assert";
import http from "node:http";
import { test } from "node:test";
import { createBridgeServer } from "../bridge.js";
import { sign } from "../hmac.js";
import { TypingManager } from "../typing.js";

const SECRET = "secreto-de-prueba-de-al-menos-32-caracteres";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function recorder(opts: { failStart?: boolean } = {}) {
  const calls: string[] = [];
  return {
    calls,
    actions: {
      typing: async (jid: string) => {
        calls.push(`typing:${jid}`);
        if (opts.failStart) throw new Error("no_chat");
      },
      clear: async (jid: string) => {
        calls.push(`clear:${jid}`);
      },
    },
  };
}

test("el indicador se activa al empezar y se renueva mientras dura el trabajo", async () => {
  const r = recorder();
  const manager = new TypingManager(r.actions, { refreshMs: 15, maxMs: 1000 });
  assert.equal(await manager.start("A@c.us"), true);
  await sleep(60);
  await manager.stop("A@c.us");
  const typingCalls = r.calls.filter((c) => c.startsWith("typing:")).length;
  assert.ok(typingCalls >= 3, `se renueva varias veces (${typingCalls})`);
});

test("al detenerlo (envío) cesa la renovación y se limpia el estado una sola vez", async () => {
  const r = recorder();
  const manager = new TypingManager(r.actions, { refreshMs: 10, maxMs: 1000 });
  await manager.start("A@c.us");
  await sleep(25);
  await manager.stop("A@c.us");
  const countAtStop = r.calls.length;
  await sleep(40);
  assert.equal(r.calls.length, countAtStop, "ninguna llamada después de detener");
  assert.equal(r.calls.filter((c) => c === "clear:A@c.us").length, 1);
  assert.equal(manager.isActive("A@c.us"), false);
});

test("nunca queda activo indefinidamente: caduca al límite máximo aunque no se detenga", async () => {
  const r = recorder();
  const manager = new TypingManager(r.actions, { refreshMs: 10, maxMs: 50 });
  await manager.start("A@c.us");
  await sleep(120);
  assert.equal(manager.isActive("A@c.us"), false, "caducó por seguridad");
  const after = r.calls.length;
  await sleep(40);
  assert.equal(r.calls.length, after, "sin renovaciones tras caducar");
  assert.ok(r.calls.includes("clear:A@c.us"));
});

test("dos clientes tienen estados independientes", async () => {
  const r = recorder();
  const manager = new TypingManager(r.actions, { refreshMs: 10, maxMs: 1000 });
  await manager.start("A@c.us");
  await manager.start("B@c.us");
  await manager.stop("A@c.us");
  const before = r.calls.filter((c) => c === "typing:B@c.us").length;
  await sleep(35);
  assert.ok(r.calls.filter((c) => c === "typing:B@c.us").length > before, "B sigue escribiendo");
  assert.equal(manager.isActive("B@c.us"), true);
  await manager.stopAll();
  assert.equal(manager.isActive("B@c.us"), false);
});

test("si WhatsApp no puede activar el estado, no queda ningún temporizador", async () => {
  const r = recorder({ failStart: true });
  const manager = new TypingManager(r.actions, { refreshMs: 10, maxMs: 1000 });
  assert.equal(await manager.start("A@c.us"), false);
  await sleep(30);
  assert.equal(manager.isActive("A@c.us"), false);
  assert.equal(r.calls.length, 1, "un único intento fallido y ninguna renovación");
});

test("volver a empezar un chat activo no duplica renovaciones", async () => {
  const r = recorder();
  const manager = new TypingManager(r.actions, { refreshMs: 20, maxMs: 1000 });
  await manager.start("A@c.us");
  await manager.start("A@c.us");
  await manager.start("A@c.us");
  await sleep(45);
  await manager.stop("A@c.us");
  const typingCalls = r.calls.filter((c) => c === "typing:A@c.us").length;
  assert.ok(typingCalls <= 4, `sin temporizadores duplicados (${typingCalls})`);
});

// ---------- Endpoint /typing del puente ----------

function startBridge(typing: any, killed = false) {
  const server = createBridgeServer({
    secret: SECRET, isEnabled: () => true, isKilled: () => killed, connectionState: () => "ready",
    sendText: async () => "x", onKill: async () => undefined, typing,
  });
  return new Promise<{ url: string; close: () => void }>((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const port = (server.address() as { port: number }).port;
      resolve({ url: `http://127.0.0.1:${port}`, close: () => server.close() });
    });
  });
}

function postTyping(url: string, body: string, signature?: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const stamp = String(Math.floor(Date.now() / 1000));
    const req = http.request(`${url}/typing`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
        "X-Bridge-Signature": signature ?? sign(SECRET, "send", stamp, body) },
    }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

test("puente: /typing activa y detiene el indicador solo para rutas conocidas y firmadas", async () => {
  const seen: string[] = [];
  const typing = {
    start: async (route: string) => (route === "912345678901" ? "started" : "unknown_route"),
    stop: async (route: string) => { seen.push(`stop:${route}`); },
  };
  const bridge = await startBridge(typing);
  try {
    const ok = await postTyping(bridge.url, JSON.stringify({ route: "912345678901", action: "start" }));
    assert.equal(ok.status, 200);
    assert.deepEqual(JSON.parse(ok.body), { typing: true });
    const unknown = await postTyping(bridge.url, JSON.stringify({ route: "911111111111", action: "start" }));
    assert.equal(unknown.status, 422);
    const stopped = await postTyping(bridge.url, JSON.stringify({ route: "912345678901", action: "stop" }));
    assert.equal(stopped.status, 200);
    assert.deepEqual(seen, ["stop:912345678901"]);
    const unsigned = await postTyping(bridge.url, JSON.stringify({ route: "912345678901", action: "start" }), "00");
    assert.equal(unsigned.status, 401);
  } finally {
    bridge.close();
  }
});

test("puente: con STOP activo /typing no muestra nada", async () => {
  const typing = { start: async () => "started", stop: async () => undefined };
  const bridge = await startBridge(typing, true);
  try {
    const r = await postTyping(bridge.url, JSON.stringify({ route: "912345678901", action: "start" }));
    assert.equal(r.status, 409);
    assert.deepEqual(JSON.parse(r.body), { error: "killed" });
  } finally {
    bridge.close();
  }
});
