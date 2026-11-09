import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createBridgeServer } from "../bridge.js";
import type { WebjsConfig } from "../config.js";
import { ChatNotResolvedError, newDiag, sendText, type GatewayDeps } from "../gateway.js";
import { sign } from "../hmac.js";
import { BoundedSet } from "../policy.js";
import { RouteTable } from "../routes.js";

const NOW = 1_800_000_000;
const SECRET = "secreto-de-prueba-de-al-menos-32-caracteres";
const LID = "123456789012345@lid";

// Tres salidas posibles de un envío: NO ENVIADO (nada llegó), ENVIADO (con identificador) e INCIERTO
// (se intentó y no hay confirmación). Cada una tiene un código HTTP y un comportamiento distinto en el worker.
function harness(opts: { resolved: boolean | "throws"; send: "ok" | "empty" | "throws" }) {
  const dir = mkdtempSync(join(tmpdir(), "send-"));
  const routes = new RouteTable(Buffer.alloc(32, 8), join(dir, "r.json"), join(dir, "i.json"), join(dir, "k.json"));
  const route = routes.routeFor(LID) as string;
  routes.markKnown(route);
  const config: WebjsConfig = {
    secret: SECRET, backendUrl: "http://127.0.0.1:8000", port: 8787, mode: "live", autoReply: true,
    inboundMode: "single_chat", readyTimeoutSec: 120, authDir: join(dir, "a"), stateDir: dir, logDir: dir,
  };
  let sendCalls = 0;
  const deps: GatewayDeps = {
    config, routes,
    policy: { seen: new BoundedSet(10), routeFor: (c) => routes.routeFor(c) },
    diag: newDiag(), state: () => "ready", isKilled: () => false, nowSec: () => NOW,
    transport: {
      sendMessage: async () => {
        sendCalls += 1;
        if (opts.send === "throws") throw new Error("socket_reset");
        if (opts.send === "empty") return undefined;
        return { id: { _serialized: "true_" + LID + "_OUT1" } };
      },
      chatResolved: async () => {
        if (opts.resolved === "throws") throw new Error("evaluate_failed");
        return opts.resolved;
      },
    },
    forward: async () => ({ ok: true, status: 200 }),
    log: () => undefined,
  };
  return { deps, route, counts: () => sendCalls };
}

test("NO ENVIADO: si WhatsApp no resuelve el chat, no se intenta el envío y el resultado es un rechazo definitivo", async () => {
  const h = harness({ resolved: false, send: "ok" });
  await assert.rejects(sendText(h.route, "hola", h.deps), (e: unknown) => e instanceof ChatNotResolvedError);
  assert.equal(h.counts(), 0, "sendMessage nunca se llama");
});

test("NO ENVIADO: si la comprobación previa falla, tampoco se envía", async () => {
  const h = harness({ resolved: "throws", send: "ok" });
  await assert.rejects(sendText(h.route, "hola", h.deps), (e: unknown) => e instanceof ChatNotResolvedError);
  assert.equal(h.counts(), 0);
});

test("INCIERTO: se intentó el envío y no hay identificador: error de resultado incierto, sin reintento aquí", async () => {
  const h = harness({ resolved: true, send: "empty" });
  await assert.rejects(sendText(h.route, "hola", h.deps), /no_message_id/);
  assert.equal(h.counts(), 1, "exactamente un intento");
});

test("INCIERTO: un error durante el envío no se reintenta", async () => {
  const h = harness({ resolved: true, send: "throws" });
  await assert.rejects(sendText(h.route, "hola", h.deps), /socket_reset/);
  assert.equal(h.counts(), 1);
});

test("ENVIADO: chat resuelto y con identificador de WhatsApp", async () => {
  const h = harness({ resolved: true, send: "ok" });
  const id = await sendText(h.route, "hola", h.deps);
  assert.equal(id, "true_" + LID + "_OUT1");
  assert.equal(h.counts(), 1);
});

// ---------- Códigos HTTP del puente: 422 no enviado, 500 incierto, 200 enviado ----------

function startBridge(sendText: (route: string, text: string) => Promise<string>) {
  const server = createBridgeServer({
    secret: SECRET, isEnabled: () => true, isKilled: () => false, connectionState: () => "ready",
    sendText, onKill: async () => undefined,
  });
  return new Promise<{ url: string; close: () => void }>((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const port = (server.address() as { port: number }).port;
      resolve({ url: `http://127.0.0.1:${port}`, close: () => server.close() });
    });
  });
}

function postSend(url: string, body: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const stamp = String(Math.floor(Date.now() / 1000));
    const req = http.request(`${url}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
        "X-Bridge-Signature": sign(SECRET, "send", stamp, body) },
    }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

const sendBody = JSON.stringify({ to: "912345678901", text: "hola", idempotency_key: "K-1" });

test("puente: chat no resuelto responde 422 chat_not_resolved (no enviado, sin reintento)", async () => {
  const bridge = await startBridge(async () => { throw new ChatNotResolvedError(); });
  try {
    const r = await postSend(bridge.url, sendBody);
    assert.equal(r.status, 422);
    assert.deepEqual(JSON.parse(r.body), { error: "chat_not_resolved" });
  } finally {
    bridge.close();
  }
});

test("puente: envío sin confirmación responde 500 (incierto) y nunca 200 sin identificador", async () => {
  const bridge = await startBridge(async () => { throw new Error("no_message_id"); });
  try {
    const r = await postSend(bridge.url, sendBody);
    assert.equal(r.status, 500);
    assert.equal(JSON.parse(r.body).error, "send_failed", "el detalle del error no se devuelve");
  } finally {
    bridge.close();
  }
});

test("puente: envío confirmado responde 200 con el identificador", async () => {
  const bridge = await startBridge(async () => "true_x_OUT2");
  try {
    const r = await postSend(bridge.url, sendBody);
    assert.equal(r.status, 200);
    assert.deepEqual(JSON.parse(r.body), { message_id: "true_x_OUT2" });
  } finally {
    bridge.close();
  }
});
