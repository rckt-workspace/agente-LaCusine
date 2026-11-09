import { strict as assert } from "node:assert";
import { test } from "node:test";
import { deadlineOf, loadConfig } from "../config.js";
import { errorSummary, pageProbe, sanitize } from "../diagnostics.js";
import { ReadyGate } from "../ready.js";

const SECRET = "secreto-de-prueba-de-al-menos-32-caracteres";
const NOW = 1_800_000_000;

test("ready: authenticated abre un plazo; sin ready al vencer, el estado es timed_out", () => {
  const gate = new ReadyGate(120_000);
  gate.authenticated(1_000);
  assert.equal(gate.check(1_000 + 119_999), "waiting_ready");
  assert.equal(gate.check(1_000 + 120_000), "timed_out");
  assert.equal(gate.current, "timed_out");
});

test("ready: una repetición de authenticated no reinicia el plazo", () => {
  const gate = new ReadyGate(60_000);
  gate.authenticated(0);
  gate.authenticated(50_000);
  gate.authenticated(59_000);
  assert.equal(gate.check(60_000), "timed_out");
});

test("ready: si ready llega dentro del plazo, nunca caduca", () => {
  const gate = new ReadyGate(60_000);
  gate.authenticated(0);
  gate.ready();
  assert.equal(gate.check(10_000_000), "ready");
});

test("ready: un timeout ya declarado no se revierte con un ready tardío", () => {
  const gate = new ReadyGate(60_000);
  gate.authenticated(0);
  gate.check(60_000);
  gate.ready();
  assert.equal(gate.current, "timed_out");
});

test("ready: el plazo debe ser positivo", () => {
  assert.throws(() => new ReadyGate(0));
});

test("saneado: enmascara números, identificadores, rutas de usuario y saltos de línea", () => {
  const out = sanitize("fallo en C:\\Users\\maura\\OneDrive\\x para 573153863179@c.us\nen /home/usuario/app");
  assert.ok(!out.includes("573153863179"), "sin número de teléfono");
  assert.ok(!out.includes("@c.us"), "sin identificador");
  assert.ok(!out.includes("maura"), "sin nombre de cuenta");
  assert.ok(!out.includes("usuario"), "sin nombre de cuenta Unix");
  assert.ok(!out.includes("\n"));
});

test("saneado: acota la longitud del texto", () => {
  assert.equal(sanitize("a".repeat(500), 40).length, 40);
});

test("errorSummary: conserva el tipo y no conserva números del mensaje", () => {
  const summary = errorSummary(new Error("sesión 573153863179 caducada"));
  assert.ok(summary.startsWith("name=Error"));
  assert.ok(!summary.includes("573153863179"));
  assert.ok(errorSummary("ready timeout").includes("thrown_string"));
});

test("sonda de página: solo booleanos y versión, aunque un módulo lance error", () => {
  const w = globalThis as any;
  w.window = {
    require: (name: string) => {
      if (name === "WAWebCmd") return {};
      if (name === "WAWebSocketModel") throw new Error("módulo ausente");
      return undefined;
    },
    Debug: { VERSION: "2.3000.1053716622" },
  };
  w.document = { readyState: "complete" };
  try {
    const probe = pageProbe();
    assert.equal(probe.wwebjs_injected, false);
    assert.equal(probe.require_available, true);
    assert.equal(probe.module_cmd, true);
    assert.equal(probe.module_socket, false);
    assert.equal(probe.module_conn, false);
    assert.equal(probe.wa_version, "2.3000.1053716622");
    assert.equal(probe.dom_ready_state, "complete");
  } finally {
    delete w.window;
    delete w.document;
  }
});

test("configuración: la observación relativa no fija fin hasta que llegue ready", () => {
  const cfg = loadConfig(
    { WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "observe", WEBJS_OBSERVE_SECONDS: "120" },
    NOW,
  );
  assert.equal(cfg.observeSeconds, 120);
  assert.equal(cfg.observeUntil, undefined);
  assert.equal(deadlineOf(cfg), undefined);
  assert.equal(cfg.readyTimeoutSec, 120, "plazo de ready por defecto");
});

test("configuración: el plazo de ready y la duración de observación están acotados", () => {
  const base = { WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "observe" };
  assert.throws(() => loadConfig({ ...base, WEBJS_OBSERVE_SECONDS: "30" }, NOW), /WEBJS_OBSERVE_SECONDS/);
  assert.throws(() => loadConfig({ ...base, WEBJS_OBSERVE_SECONDS: "901" }, NOW), /WEBJS_OBSERVE_SECONDS/);
  assert.throws(() => loadConfig({ ...base, WEBJS_OBSERVE_SECONDS: "120", WEBJS_READY_TIMEOUT_SEC: "10" }, NOW), /WEBJS_READY_TIMEOUT_SEC/);
  assert.throws(() => loadConfig({ ...base, WEBJS_OBSERVE_SECONDS: "120", WEBJS_READY_TIMEOUT_SEC: "601" }, NOW), /WEBJS_READY_TIMEOUT_SEC/);
  assert.equal(loadConfig({ ...base, WEBJS_OBSERVE_SECONDS: "120", WEBJS_READY_TIMEOUT_SEC: "45" }, NOW).readyTimeoutSec, 45);
});

test("configuración: observar sin duración alguna se rechaza y no admite respuestas automáticas", () => {
  assert.throws(() => loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "observe" }, NOW), /WEBJS_OBSERVE_UNTIL/);
  assert.throws(
    () => loadConfig({ WEB_BRIDGE_SECRET: SECRET, WEBJS_MODE: "observe", WEBJS_OBSERVE_SECONDS: "120", WEB_BRIDGE_AUTO_REPLY: "true" }, NOW),
    /no admite WEB_BRIDGE_AUTO_REPLY/,
  );
});
