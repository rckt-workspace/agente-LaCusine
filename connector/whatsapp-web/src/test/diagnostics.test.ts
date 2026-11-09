import { strict as assert } from "node:assert";
import type { AddressInfo } from "node:net";
import { test } from "node:test";
import { createBridgeServer } from "../bridge.js";
import { sign } from "../hmac.js";

const SECRET = "s3cr3t-de-prueba-de-32-caracteres!!";

// /status debe exponer contadores de solo lectura, sin contenido, y mantener la firma obligatoria.
test("status incluye diagnósticos firmados y no admite solicitudes sin firma", async () => {
  const server = createBridgeServer({
    secret: SECRET,
    isEnabled: () => false,
    isKilled: () => false,
    connectionState: () => "open",
    diagnostics: () => ({ connection_state: "open", ready: false, raw_upserts_total: 3, stop_blocked_total: 1 }),
    sendText: async () => "wamid.X",
    onKill: async () => {},
    onUnlink: async () => {},
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const { port } = server.address() as AddressInfo;
  const base = `http://127.0.0.1:${port}/status`;
  try {
    const unsigned = await fetch(base);
    assert.equal(unsigned.status, 401);

    const stamp = String(Math.floor(Date.now() / 1000));
    const signed = await fetch(base, {
      headers: { "X-Bridge-Timestamp": stamp, "X-Bridge-Signature": sign(SECRET, "status", stamp, "") },
    });
    assert.equal(signed.status, 200);
    const body = (await signed.json()) as Record<string, unknown>;
    assert.equal(body.connection, "open");
    assert.equal(body.raw_upserts_total, 3);
    assert.equal(body.stop_blocked_total, 1);
    assert.equal(body.ready, false);
    // Ningún campo debe contener números de teléfono ni texto de mensajes.
    assert.ok(!JSON.stringify(body).match(/[0-9]{8,}/), "no debe aparecer ningún número largo");
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
