import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { WebjsConfig } from "../config.js";
import type { ForwardResult } from "../forwarder.js";
import { correlation, newDiag, onIncoming, type GatewayDeps } from "../gateway.js";
import { BoundedSet } from "../policy.js";
import { RouteTable } from "../routes.js";

const NOW = 1_800_000_000;
const CLIENT = "573001112233";

function harness(result: ForwardResult) {
  const dir = mkdtempSync(join(tmpdir(), "fwd-"));
  const routes = new RouteTable(Buffer.alloc(32, 9), join(dir, "r.json"), join(dir, "i.json"), join(dir, "k.json"));
  const config: WebjsConfig = {
    secret: "secreto-de-prueba-de-al-menos-32-caracteres", backendUrl: "http://127.0.0.1:8000", port: 8787,
    mode: "live", autoReply: true, inboundMode: "single_chat", readyTimeoutSec: 120, authDir: join(dir, "a"),
    stateDir: dir, logDir: dir,
  };
  const logs: string[] = [];
  const diag = newDiag();
  const deps: GatewayDeps = {
    config, routes,
    policy: { seen: new BoundedSet(10), routeFor: (c) => routes.routeFor(c) },
    diag, state: () => "ready", isKilled: () => false, nowSec: () => NOW,
    transport: { sendMessage: async () => undefined },
    forward: async () => result,
    log: (l) => logs.push(l),
  };
  return { deps, logs, diag };
}

const msg = { fromMe: false, from: `${CLIENT}@c.us`, type: "chat", body: "Hola", id: "false_c_F1", timestampSec: NOW - 5 };

test("un 200 que descarta el mensaje NO cuenta como reenviado ni aparece como aceptado", async () => {
  const h = harness({ ok: true, status: 200, items: [{ correlation_id: correlation(msg.id), outcome: "rejected", reason: "message_too_old" }] });
  await onIncoming(msg, h.deps);
  assert.equal(h.diag.messagesForwardedTotal, 0);
  assert.ok(h.logs.some((l) => l.includes("stage=03_backend_forward_not_persisted") && l.includes("outcome=rejected") && l.includes("reason=message_too_old")));
  assert.ok(!h.logs.some((l) => l.includes("stage=03_backend_forward_accepted")));
});

test("un 200 sin desglose por elemento NO se da por guardado", async () => {
  const h = harness({ ok: true, status: 200 });
  await onIncoming(msg, h.deps);
  assert.equal(h.diag.messagesForwardedTotal, 0);
  assert.ok(h.logs.some((l) => l.includes("outcome=unknown")));
});

test("aceptado y duplicado cuentan como persistidos; un conflicto no", async () => {
  const accepted = harness({ ok: true, status: 200, items: [{ correlation_id: correlation(msg.id), outcome: "accepted" }] });
  await onIncoming(msg, accepted.deps);
  assert.equal(accepted.diag.messagesForwardedTotal, 1);

  const duplicate = harness({ ok: true, status: 200, items: [{ correlation_id: correlation(msg.id), outcome: "duplicate" }] });
  await onIncoming(msg, duplicate.deps);
  assert.equal(duplicate.diag.messagesForwardedTotal, 1);

  const conflict = harness({ ok: true, status: 200, items: [{ correlation_id: correlation(msg.id), outcome: "conflict" }] });
  await onIncoming(msg, conflict.deps);
  assert.equal(conflict.diag.messagesForwardedTotal, 0);
});

test("un HTTP de error sigue siendo fallo explícito", async () => {
  const h = harness({ ok: false, status: 503 });
  await onIncoming(msg, h.deps);
  assert.equal(h.diag.messagesForwardedTotal, 0);
  assert.ok(h.logs.some((l) => l.includes("stage=03_backend_forward_failed http=503")));
});

test("atención pública: una ruta solo queda autorizada si el backend admite el mensaje", async () => {
  const rejected = harness({ ok: true, status: 200, items: [{ correlation_id: correlation(msg.id), outcome: "rejected", reason: "message_too_old" }] });
  await onIncoming(msg, rejected.deps);
  assert.equal(rejected.deps.routes.knownRoute(`${CLIENT}`), false, "rechazada: sin autorización de respuesta");

  const accepted = harness({ ok: true, status: 200, items: [{ correlation_id: correlation(msg.id), outcome: "accepted" }] });
  await onIncoming(msg, accepted.deps);
  assert.equal(accepted.deps.routes.knownRoute(CLIENT), true, "admitida: respuesta autorizada");
});

test("atención pública: un rechazo posterior no retira la autorización de una ruta ya admitida", async () => {
  const dir = mkdtempSync(join(tmpdir(), "fwd-keep-"));
  const routes = new RouteTable(Buffer.alloc(32, 7), join(dir, "r.json"), join(dir, "i.json"), join(dir, "k.json"));
  routes.markKnown(CLIENT);
  let result: ForwardResult = { ok: true, status: 200, items: [{ correlation_id: correlation(msg.id), outcome: "rejected", reason: "x" }] };
  const deps: GatewayDeps = {
    config: harness({ ok: true, status: 200 }).deps.config, routes,
    policy: { seen: new BoundedSet(10), routeFor: (c) => routes.routeFor(c) },
    diag: newDiag(), state: () => "ready", isKilled: () => false, nowSec: () => NOW,
    transport: { sendMessage: async () => undefined },
    forward: async () => result, log: () => undefined,
  };
  await onIncoming({ ...msg, id: "false_c_F2" }, deps);
  assert.equal(routes.knownRoute(CLIENT), true);
  result = { ok: true, status: 200 };
});
