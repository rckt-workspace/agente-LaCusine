import { strict as assert } from "node:assert";
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { forwardAllowed, loadConfig, type ConnectorConfig } from "../config.js";
import { acquireLock, releaseLock } from "../lock.js";

const SECRET = "s3cr3t-de-prueba-de-32-caracteres!!";
const NOW = 1_800_000_000;
const base = { WEB_BRIDGE_SECRET: SECRET };

test("observación: arranca sin respuestas automáticas y con ventana válida", () => {
  const c = loadConfig({ ...base, WEB_BRIDGE_OBSERVE_ONLY: "true", WEB_BRIDGE_OBSERVE_UNTIL: String(NOW + 600) }, NOW);
  assert.equal(c.observeOnly, true);
  assert.equal(c.autoReply, false);
  assert.equal(c.observeUntil, NOW + 600);
});

test("observación: rechaza WEB_BRIDGE_AUTO_REPLY=true aunque se pida por error", () => {
  assert.throws(
    () => loadConfig({ ...base, WEB_BRIDGE_OBSERVE_ONLY: "true", WEB_BRIDGE_AUTO_REPLY: "true",
      WEB_BRIDGE_OBSERVE_UNTIL: String(NOW + 600) }, NOW),
    /no admite WEB_BRIDGE_AUTO_REPLY=true/,
  );
});

test("observación: exige una caducidad futura y no superior a 15 minutos", () => {
  assert.throws(() => loadConfig({ ...base, WEB_BRIDGE_OBSERVE_ONLY: "true" }, NOW), /WEB_BRIDGE_OBSERVE_UNTIL/);
  assert.throws(
    () => loadConfig({ ...base, WEB_BRIDGE_OBSERVE_ONLY: "true", WEB_BRIDGE_OBSERVE_UNTIL: String(NOW - 1) }, NOW),
    /WEB_BRIDGE_OBSERVE_UNTIL/,
  );
  assert.throws(
    () => loadConfig({ ...base, WEB_BRIDGE_OBSERVE_ONLY: "true", WEB_BRIDGE_OBSERVE_UNTIL: String(NOW + 901) }, NOW),
    /15 minutos/,
  );
});

test("sin observación la configuración es la de siempre", () => {
  const c = loadConfig(base, NOW);
  assert.equal(c.observeOnly, false);
  assert.equal(c.observeUntil, undefined);
});

test("reenvío: la observación bloquea siempre, incluso con respuestas activas en la configuración", () => {
  const c = { ...loadConfig(base, NOW), observeOnly: true, autoReply: true } as ConnectorConfig;
  assert.deepEqual(forwardAllowed(c, NOW), { allowed: false, reason: "observe_only" });
});

test("reenvío: sin respuestas automáticas o con la ventana cerrada no se reenvía", () => {
  const off = loadConfig(base, NOW);
  assert.deepEqual(forwardAllowed(off, NOW), { allowed: false, reason: "auto_reply_off" });

  const universal = loadConfig({ ...base, WEB_BRIDGE_AUTO_REPLY: "true", WEB_BRIDGE_INBOUND_MODE: "web_inbound_all",
    WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 60) }, NOW);
  assert.deepEqual(forwardAllowed(universal, NOW), { allowed: true });
  assert.deepEqual(forwardAllowed(universal, NOW + 120), { allowed: false, reason: "window_closed" });
});

test("cerrojo: un segundo conector con la sesión viva es rechazado", () => {
  const dir = mkdtempSync(join(tmpdir(), "obs-lock-"));
  const path = join(dir, "observe.lock");
  assert.deepEqual(acquireLock(path, 100, () => true), { ok: true });
  assert.deepEqual(acquireLock(path, 200, () => true), { ok: false, reason: "lock_held" });
  releaseLock(path, 200); // otro proceso no puede liberar el cerrojo ajeno
  assert.ok(existsSync(path));
  releaseLock(path, 100);
  assert.ok(!existsSync(path));
});

test("cerrojo: un cerrojo obsoleto (proceso muerto) se reemplaza", () => {
  const dir = mkdtempSync(join(tmpdir(), "obs-lock-"));
  const path = join(dir, "observe.lock");
  writeFileSync(path, "4242");
  assert.deepEqual(acquireLock(path, 300, () => false), { ok: true });
  assert.equal(readFileSync(path, "utf8"), "300");
});
