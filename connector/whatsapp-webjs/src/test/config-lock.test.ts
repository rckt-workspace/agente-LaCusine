import { strict as assert } from "node:assert";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { deadlineOf, forwardAllowed, loadConfig, sendAllowed } from "../config.js";
import { acquireLock, releaseLock } from "../lock.js";

const NOW = 1_800_000_000;
const base = { WEB_BRIDGE_SECRET: "s3cr3t-de-prueba-de-32-caracteres!!" };

test("configuración por defecto: modo en vivo, sin respuestas automáticas y sin ventana", () => {
  const c = loadConfig(base, NOW);
  assert.equal(c.mode, "live");
  assert.equal(c.autoReply, false);
  assert.equal(c.inboundMode, "single_chat");
  assert.equal(deadlineOf(c), undefined);
  assert.equal(sendAllowed(c, NOW), false);
});

test("configuración: la ventana universal exige respuestas activadas y caducidad futura de máximo 15 minutos", () => {
  assert.throws(() => loadConfig({ ...base, WEB_BRIDGE_INBOUND_MODE: "web_inbound_all", WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 60) }, NOW), /requiere WEB_BRIDGE_AUTO_REPLY/);
  assert.throws(() => loadConfig({ ...base, WEB_BRIDGE_AUTO_REPLY: "true", WEB_BRIDGE_INBOUND_MODE: "web_inbound_all", WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 901) }, NOW), /15 minutos/);
  const c = loadConfig({ ...base, WEB_BRIDGE_AUTO_REPLY: "true", WEB_BRIDGE_INBOUND_MODE: "web_inbound_all", WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 300) }, NOW);
  assert.equal(deadlineOf(c), NOW + 300);
  assert.equal(forwardAllowed(c, NOW).allowed, true);
  assert.deepEqual(forwardAllowed(c, NOW + 301), { allowed: false, reason: "window_closed" });
  assert.equal(sendAllowed(c, NOW + 301), false);
});

test("configuración: el modo en vivo de un solo chat envía solo con respuestas activadas", () => {
  const off = loadConfig(base, NOW);
  assert.equal(sendAllowed(off, NOW), false);
  const on = loadConfig({ ...base, WEB_BRIDGE_AUTO_REPLY: "true" }, NOW);
  assert.equal(sendAllowed(on, NOW), true);
});

test("configuración: un valor de modo desconocido se rechaza", () => {
  assert.throws(() => loadConfig({ ...base, WEBJS_MODE: "public" }, NOW), /WEBJS_MODE/);
});

test("configuración: el secreto debe tener al menos 32 caracteres", () => {
  assert.throws(() => loadConfig({ WEB_BRIDGE_SECRET: "corto" }, NOW), /32 caracteres/);
});

test("cerrojo: una segunda instancia con la sesión viva no arranca", () => {
  const path = join(mkdtempSync(join(tmpdir(), "wjs-lock-")), "webjs.lock");
  assert.deepEqual(acquireLock(path, 100, () => true), { ok: true });
  assert.deepEqual(acquireLock(path, 200, () => true), { ok: false, reason: "lock_held" });
  releaseLock(path, 200);
  assert.ok(existsSync(path), "otro proceso no puede liberar el cerrojo ajeno");
  releaseLock(path, 100);
  assert.ok(!existsSync(path));
});

test("cerrojo: uno obsoleto se reemplaza", () => {
  const path = join(mkdtempSync(join(tmpdir(), "wjs-lock-")), "webjs.lock");
  writeFileSync(path, "4242");
  assert.deepEqual(acquireLock(path, 300, () => false), { ok: true });
  assert.equal(readFileSync(path, "utf8"), "300");
});
