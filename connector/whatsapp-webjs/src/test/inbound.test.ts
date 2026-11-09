import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { WebjsConfig } from "../config.js";
import { classifyInbound, BoundedSet } from "../policy.js";
import { fieldsLine, missingRequirements, normalizeMessage, messageIdOf } from "../inbound.js";
import { newDiag, onIncoming, type GatewayDeps } from "../gateway.js";
import { RouteTable } from "../routes.js";

const NOW = 1_800_000_000;
const PHONE = "573153863179";
const LID = "123456789012345@lid";
const SECRET = "secreto-de-prueba-de-al-menos-32-caracteres";

// Objeto con la forma de Message de whatsapp-web.js 1.34.7 (id como objeto MessageId, from/to como cadenas).
function realMessage(over: Record<string, unknown> = {}): any {
  const from = PHONE + "@c.us";
  return {
    id: { fromMe: false, remote: from, id: "3EB0ABCDEF", _serialized: `false_${from}_3EB0ABCDEF` },
    from,
    to: "573000000000@c.us",
    fromMe: false,
    type: "chat",
    body: "Hola, quisiera una cotización",
    timestamp: NOW - 5,
    broadcast: false,
    isStatus: false,
    ...over,
  };
}

function policy() {
  return { seen: new BoundedSet(100), routeFor: (chatId: string) => routes.routeFor(chatId) };
}

const dir = mkdtempSync(join(tmpdir(), "inbound-"));
const routes = new RouteTable(Buffer.alloc(32, 9), join(dir, "routes.json"));

function classify(msg: any) {
  const { facts } = normalizeMessage(msg);
  return classifyInbound(facts, policy(), NOW);
}

test("entrada real @c.us: el ID serializado se usa tal cual y el mensaje se reenvía", () => {
  const d = classify(realMessage());
  assert.equal(d.action, "forward");
  if (d.action === "forward") {
    assert.equal(d.messageId, `false_${PHONE}@c.us_3EB0ABCDEF`);
    assert.equal(d.route, PHONE, "chat con número visible: el identificador son los dígitos");
    assert.equal(d.text, "Hola, quisiera una cotización", "el texto íntegro llega al agente");
  }
});

test("entrada real @lid sin _serialized: el ID se reconstruye desde fromMe/remote/id documentados", () => {
  const msg = realMessage({
    from: LID,
    id: { fromMe: false, remote: LID, id: "3EB0LID0001" },
  });
  const { facts, indicators } = normalizeMessage(msg);
  assert.equal(indicators.id_source, "reconstructed");
  assert.equal(facts.id, `false_${LID}_3EB0LID0001`);
  facts.identity = "573153863179"; // identidad verificada por WhatsApp (verifyLidIdentity)
  const d = classifyInbound(facts, policy(), NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") {
    assert.ok(/^9[0-9]{11}$/.test(d.route), "@lid: identificador opaco, nunca un número inventado");
    assert.notEqual(d.route, PHONE);
  }
});

test("ID con remote como objeto: se reconstruye igual", () => {
  const id = messageIdOf({ fromMe: false, remote: { _serialized: LID }, id: "XYZ" });
  assert.equal(id.value, `false_${LID}_XYZ`);
  assert.equal(id.source, "reconstructed");
});

test("ID realmente ausente: no se inventa; se marca message_id como requisito faltante", () => {
  const msg = realMessage({ id: undefined });
  const { facts, indicators } = normalizeMessage(msg);
  assert.equal(facts.id, null);
  assert.equal(indicators.id_source, "none");
  assert.deepEqual(missingRequirements(indicators), ["message_id"]);
  assert.equal(classify(msg).action, "ignore");
});

test("timestamp en milisegundos o no numérico: inválido, nunca se corrige", () => {
  const ms = normalizeMessage(realMessage({ timestamp: (NOW - 5) * 1000 }));
  assert.equal(ms.indicators.timestamp_valid, false);
  assert.deepEqual(missingRequirements(ms.indicators), ["timestamp"]);
  assert.equal(ms.facts.timestampSec, null);
  const str = normalizeMessage(realMessage({ timestamp: String(NOW - 5) }));
  assert.equal(str.indicators.timestamp_valid, false);
});

test("sin 'from' no se usa 'to' como sustituto", () => {
  const { facts, indicators } = normalizeMessage(realMessage({ from: undefined }));
  assert.equal(facts.from, null);
  assert.equal(indicators.has_chat_id, false);
  assert.deepEqual(missingRequirements(indicators), ["chat_id"]);
});

test("from como objeto con _serialized se acepta", () => {
  const { facts } = normalizeMessage(realMessage({ from: { _serialized: PHONE + "@c.us" } }));
  assert.equal(facts.from, PHONE + "@c.us");
});

test("las líneas de diagnóstico no contienen identificador, número ni texto", () => {
  const { indicators } = normalizeMessage(realMessage());
  const line = fieldsLine(indicators);
  assert.ok(line.startsWith("stage=01_fields "));
  assert.ok(line.includes("has_message_id=true") && line.includes("has_body=true") && line.includes("timestamp_valid=true"));
  assert.ok(!line.includes(PHONE));
  assert.ok(!line.includes("3EB0ABCDEF"));
  assert.ok(!line.includes("cotización"));
});

test("mensajes propios, grupos, estados, no texto, históricos y duplicados se descartan", () => {
  assert.equal(classify(realMessage({ fromMe: true })).action === "ignore" && (classify(realMessage({ fromMe: true })) as any).reason, "own_message");
  assert.equal((classify(realMessage({ from: "120363000000000000@g.us" })) as any).reason, "group");
  assert.equal((classify(realMessage({ from: "status@broadcast", isStatus: true })) as any).reason, "status_or_broadcast");
  assert.equal((classify(realMessage({ type: "image" })) as any).reason, "not_text");
  assert.equal((classify(realMessage({ timestamp: NOW - 3600 })) as any).reason, "too_old");
  const shared = policy();
  const m = realMessage();
  const first = classifyInbound(normalizeMessage(m).facts, shared, NOW);
  const second = classifyInbound(normalizeMessage(m).facts, shared, NOW);
  assert.equal(first.action, "forward");
  assert.equal((second as any).reason, "duplicate");
});

// ---------- Flujo completo hasta el reenvío (backend simulado) ----------
function harness(over: Partial<WebjsConfig> = {}) {
  const logs: string[] = [];
  const forwarded: unknown[] = [];
  let killed = false;
  const config: WebjsConfig = {
    secret: SECRET,
    backendUrl: "http://127.0.0.1:8000",
    port: 8787,
    mode: "live",
    autoReply: true,
    inboundMode: "single_chat",
    readyTimeoutSec: 120,
    authDir: join(dir, "auth"),
    stateDir: dir,
    logDir: dir,
    ...over,
  };
  const deps: GatewayDeps = {
    config,
    routes,
    policy: policy(),
    diag: newDiag(),
    state: () => "ready",
    isKilled: () => killed,
    nowSec: () => NOW,
    transport: { sendMessage: async () => null },
    forward: async (items) => {
      forwarded.push(...items);
      return { ok: true, status: 200 } as any;
    },
    log: (line) => logs.push(line),
  };
  return {
    deps,
    logs,
    forwarded,
    setKilled: (v: boolean) => {
      killed = v;
    },
  };
}

async function deliver(h: ReturnType<typeof harness>, msg: any) {
  const { facts, indicators } = normalizeMessage(msg);
  await onIncoming(facts, h.deps, { line: fieldsLine(indicators), missing: missingRequirements(indicators) });
}

test("mensaje válido y autorizado: stage 00, stage 01 forward y exactamente un reenvío", async () => {
  const h = harness();
  await deliver(h, realMessage());
  assert.ok(h.logs.some((l) => l.startsWith("stage=00_message_received")));
  assert.ok(h.logs.some((l) => l.includes("stage=01_classified") && l.includes("decision=forward")));
  assert.equal(h.forwarded.length, 1);
  await deliver(h, realMessage());
  assert.equal(h.forwarded.length, 1, "duplicado no se reenvía de nuevo");
});

test("con STOP: el mensaje se clasifica como forward pero no sale al backend", async () => {
  const h = harness();
  h.setKilled(true);
  await deliver(h, realMessage());
  assert.ok(h.logs.some((l) => l.includes("decision=forward")));
  assert.ok(h.logs.some((l) => l.includes("reason=stop_active")));
  assert.equal(h.forwarded.length, 0);
  assert.equal(h.deps.diag.stopBlockedTotal, 1);
});

test("observación nunca reenvía aunque el mensaje sea válido", async () => {
  const h = harness({ mode: "observe", autoReply: false, observeSeconds: 60 } as any);
  await deliver(h, realMessage());
  assert.equal(h.forwarded.length, 0);
  assert.ok(h.logs.some((l) => l.includes("reason=observe_only")));
});

test("un ID ausente no llega al backend ni se convierte en válido", async () => {
  const h = harness();
  await deliver(h, realMessage({ id: undefined }));
  assert.equal(h.forwarded.length, 0);
  assert.ok(h.logs.some((l) => l.includes("stage=01_missing requirements=message_id")));
});

test("un @lid sin identidad verificada no se reenvía, aunque el mensaje declare un número", () => {
  // El número declarado en el mensaje nunca se usa: sin verificación de WhatsApp, el chat se descarta.
  const d = classify(realMessage({ from: LID, id: { fromMe: false, remote: LID, id: "OTRO1" }, to: PHONE + "@c.us" }));
  assert.deepEqual(d, { action: "ignore", reason: "identity_unverified" });
});

test("ID en la variante $1: se acepta solo con el formato exacto <fromMe>_<remote>_<id>", () => {
  const ok = messageIdOf({ $1: `false_${LID}_3EB0DOLLAR` });
  assert.equal(ok.value, `false_${LID}_3EB0DOLLAR`);
  assert.equal(ok.source, "dollar");
  assert.equal(messageIdOf({ $1: "no-es-un-id" }).value, null, "formato desconocido: no se usa");
  assert.equal(messageIdOf({ $1: "true_abc" }).value, null, "formato incompleto: no se usa");
});

test("regresión @lid con ID en $1: el mensaje se normaliza y se reenvía con su identidad", () => {
  const msg = realMessage({ from: LID, id: { fromMe: false, remote: LID, id: "3EB0DOLLAR", $1: `false_${LID}_3EB0DOLLAR` } });
  const { facts, indicators } = normalizeMessage(msg);
  assert.equal(indicators.id_source, "dollar");
  facts.identity = "573153863179";
  const d = classifyInbound(facts, policy(), NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") assert.equal(d.messageId, `false_${LID}_3EB0DOLLAR`);
});
