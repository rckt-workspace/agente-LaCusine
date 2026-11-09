import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createBridgeServer } from "../bridge.js";
import { verifyLidIdentity } from "../identity.js";
import { sign } from "../hmac.js";
import { RouteTable } from "../routes.js";

const LID = "123456789012345@lid";
const PHONE = "573153863179";
const SECRET = "secreto-de-prueba-de-al-menos-32-caracteres";

const ok = (entry: object) => async () => [entry];

test("identidad: el número sale del mapeo de WhatsApp cuando lid y pn coinciden", async () => {
  const out = await verifyLidIdentity(LID, ok({ lid: LID, pn: `${PHONE}@c.us` }));
  assert.deepEqual(out, { ok: true, phone: PHONE });
});

test("identidad: fallo del método, plazo vencido, respuesta ambigua o vacía se rechazan", async () => {
  assert.deepEqual(await verifyLidIdentity(LID, async () => { throw new Error("boom"); }), { ok: false, reason: "lookup_failed" });
  const hang = () => new Promise<never>(() => undefined);
  assert.deepEqual(await verifyLidIdentity(LID, hang, 20), { ok: false, reason: "lookup_timeout" });
  assert.deepEqual(await verifyLidIdentity(LID, async () => [{ lid: LID, pn: `${PHONE}@c.us` }, { lid: LID }]), { ok: false, reason: "ambiguous" });
  assert.deepEqual(await verifyLidIdentity(LID, async () => []), { ok: false, reason: "ambiguous" });
});

test("identidad: un lid distinto del chat (contradicción) se rechaza", async () => {
  const out = await verifyLidIdentity(LID, ok({ lid: "999999999999999@lid", pn: `${PHONE}@c.us` }));
  assert.deepEqual(out, { ok: false, reason: "lid_mismatch" });
});

test("identidad: sin mapeo telefónico o con un formato no válido se rechaza", async () => {
  assert.deepEqual(await verifyLidIdentity(LID, ok({ lid: LID, pn: null })), { ok: false, reason: "no_phone_mapping" });
  assert.deepEqual(await verifyLidIdentity(LID, ok({ lid: LID, pn: `${PHONE}@lid` })), { ok: false, reason: "phone_format" });
  assert.deepEqual(await verifyLidIdentity(LID, ok({ lid: LID, pn: "12@c.us" })), { ok: false, reason: "phone_format" });
});

test("rutas: un @lid no tiene identidad hasta que WhatsApp la verifica, y el número no se deriva del LID", () => {
  const dir = mkdtempSync(join(tmpdir(), "ident-"));
  const table = new RouteTable(Buffer.alloc(32, 3), join(dir, "routes.json"), join(dir, "identities.json"));
  const route = table.routeFor(LID) as string;
  assert.match(route, /^9[0-9]{11}$/);
  assert.equal(table.identityFor(route), null);
  assert.equal(table.recordIdentity(route, PHONE), true);
  assert.equal(table.identityFor(route), PHONE);
  assert.equal(table.recordIdentity(route, "573009998877"), false, "no se sobrescribe una identidad distinta");
  assert.equal(table.identityFor(PHONE), PHONE, "@c.us: el propio número");
});

test("rutas: la identidad verificada sobrevive a un reinicio", () => {
  const dir = mkdtempSync(join(tmpdir(), "ident-"));
  const first = new RouteTable(Buffer.alloc(32, 4), join(dir, "routes.json"), join(dir, "identities.json"));
  const route = first.routeFor(LID) as string;
  first.recordIdentity(route, PHONE);
  const again = new RouteTable(Buffer.alloc(32, 4), join(dir, "routes.json"), join(dir, "identities.json"));
  assert.equal(again.identityFor(route), PHONE);
  assert.equal(again.jidFor(route), LID, "la ruta de respuesta vuelve al mismo chat");
});

function startBridge(identityOf: (route: string) => string | null) {
  const server = createBridgeServer({
    secret: SECRET,
    isEnabled: () => false,
    isKilled: () => false,
    connectionState: () => "ready",
    identityOf,
    sendText: async () => "x",
    onKill: async () => undefined,
  });
  return new Promise<{ url: string; close: () => void }>((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const address = server.address() as { port: number };
      resolve({ url: `http://127.0.0.1:${address.port}`, close: () => server.close() });
    });
  });
}

function postIdentity(url: string, body: string, signature?: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const stamp = String(Math.floor(Date.now() / 1000));
    const req = http.request(`${url}/identity`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-Timestamp": stamp,
        "X-Bridge-Signature": signature ?? sign(SECRET, "status", stamp, body),
      },
    }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

test("puente: /identity devuelve solo identidades verificadas, firmadas y sin cuerpo inesperado", async () => {
  const bridge = await startBridge((route) => (route === "912345678901" ? PHONE : null));
  try {
    const known = await postIdentity(bridge.url, JSON.stringify({ route: "912345678901" }));
    assert.equal(known.status, 200);
    assert.deepEqual(JSON.parse(known.body), { phone: PHONE });
    const unknown = await postIdentity(bridge.url, JSON.stringify({ route: "911111111111" }));
    assert.equal(unknown.status, 404);
    const unsigned = await postIdentity(bridge.url, JSON.stringify({ route: "912345678901" }), "00");
    assert.equal(unsigned.status, 401);
    const broken = await postIdentity(bridge.url, "{no-json");
    assert.equal(broken.status, 400);
  } finally {
    bridge.close();
  }
});
