import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { WebjsConfig } from "../config.js";
import { newDiag, sendText, type GatewayDeps } from "../gateway.js";
import { BoundedSet } from "../policy.js";
import { RouteTable } from "../routes.js";

const NOW = 1_800_000_000;
const LID = "123456789012345@lid";

// Caso real de la librería: sendMessage ejecuta el envío y luego devuelve vacío si no encuentra el modelo local.
function harness(opts: {
  sendResult: unknown;
  recover?: "found" | "none" | "throws";
}) {
  const dir = mkdtempSync(join(tmpdir(), "recover-"));
  const routes = new RouteTable(Buffer.alloc(32, 4), join(dir, "r.json"), join(dir, "i.json"), join(dir, "k.json"));
  const route = routes.routeFor(LID) as string;
  routes.markKnown(route);
  const config: WebjsConfig = {
    secret: "secreto-de-prueba-de-al-menos-32-caracteres", backendUrl: "http://127.0.0.1:8000", port: 8787,
    mode: "live", autoReply: true, inboundMode: "single_chat", readyTimeoutSec: 120, authDir: join(dir, "a"),
    stateDir: dir, logDir: dir,
  };
  const logs: string[] = [];
  let sends = 0;
  let recoverCalls = 0;
  const deps: GatewayDeps = {
    config, routes,
    policy: { seen: new BoundedSet(10), routeFor: (c) => routes.routeFor(c) },
    diag: newDiag(), state: () => "ready", isKilled: () => false, nowSec: () => NOW,
    transport: {
      sendMessage: async () => {
        sends += 1;
        return opts.sendResult as any;
      },
      recoverSentId: async () => {
        recoverCalls += 1;
        if (opts.recover === "throws") throw new Error("fetch_failed");
        return opts.recover === "found" ? "true_" + LID + "_RECOVERED" : null;
      },
    },
    forward: async () => ({ ok: true, status: 200 }),
    log: (l) => logs.push(l),
  };
  return { deps, route, logs, sends: () => sends, recoverCalls: () => recoverCalls };
}

test("el envío ocurrió sin identificador devuelto: se confirma con el mensaje propio y no se reenvía", async () => {
  const h = harness({ sendResult: undefined, recover: "found" });
  const id = await sendText(h.route, "Respuesta", h.deps);
  assert.equal(id, "true_" + LID + "_RECOVERED");
  assert.equal(h.sends(), 1, "un único envío");
  assert.equal(h.deps.diag.sendsTotal, 1);
  assert.ok(h.logs.includes("stage=05_send_recovered"));
});

test("sin identificador ni mensaje propio confirmado: el resultado es INCIERTO, sin reintento", async () => {
  const h = harness({ sendResult: undefined, recover: "none" });
  await assert.rejects(sendText(h.route, "Respuesta", h.deps), /no_message_id/);
  assert.equal(h.sends(), 1, "nunca se vuelve a enviar");
  assert.ok(h.logs.some((l) => l.includes("stage=05_no_message_id result=undefined")));
});

test("si la propia recuperación falla, el resultado sigue siendo incierto (nunca confirmado a ciegas)", async () => {
  const h = harness({ sendResult: { id: {} }, recover: "throws" });
  await assert.rejects(sendText(h.route, "Respuesta", h.deps), /no_message_id/);
  assert.equal(h.sends(), 1);
});

test("con identificador devuelto por la librería no se busca nada más", async () => {
  const h = harness({ sendResult: { id: { _serialized: "true_" + LID + "_DIRECT" } } });
  const id = await sendText(h.route, "Respuesta", h.deps);
  assert.equal(id, "true_" + LID + "_DIRECT");
  assert.equal(h.recoverCalls(), 0);
});
