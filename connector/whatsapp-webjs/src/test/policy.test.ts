import { strict as assert } from "node:assert";
import { test } from "node:test";
import { BoundedSet, classifyInbound, type InboundFacts, type PolicyState } from "../policy.js";
import { RouteTable } from "../routes.js";
import { randomBytes } from "node:crypto";

const NOW = 1_800_000_000;
const CLIENT = "573001112233@c.us";
const LID = "123456789012345@lid";

function state(): PolicyState {
  const routes = new RouteTable(randomBytes(32));
  return { seen: new BoundedSet(100), routeFor: (chatId) => routes.routeFor(chatId) };
}

function live(overrides: Partial<InboundFacts> = {}): InboundFacts {
  return {
    fromMe: false,
    from: CLIENT,
    type: "chat",
    body: "Hola, quiero cotizar",
    id: "ABC123",
    timestampSec: NOW - 5,
    broadcast: false,
    isStatus: false,
    ...overrides,
  };
}

test("un mensaje nuevo de texto de un chat individual se reenvía", () => {
  const d = classifyInbound(live(), state(), NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") {
    assert.equal(d.route, "573001112233");
    assert.equal(d.chatId, CLIENT);
    assert.equal(d.text, "Hola, quiero cotizar");
  }
});

test("un mensaje de un contacto con dirección @lid se reenvía con identificador opaco y conserva el chat original", () => {
  const st = state();
  const d = classifyInbound(live({ from: LID, identity: "573153863179" }), st, NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") {
    assert.match(d.route, /^9[0-9]{11}$/);
    assert.equal(d.chatId, LID);
    assert.equal(d.identity, "573153863179", "la identidad verificada acompaña a la ruta");
    assert.ok(!LID.includes(d.route), "el identificador no se deriva de la dirección");
  }
});

test("un @lid sin identidad verificada, o con una identidad no válida, nunca se reenvía", () => {
  assert.deepEqual(classifyInbound(live({ from: LID }), state(), NOW), { action: "ignore", reason: "identity_unverified" });
  assert.deepEqual(classifyInbound(live({ from: LID, identity: null }), state(), NOW), { action: "ignore", reason: "identity_unverified" });
  assert.deepEqual(classifyInbound(live({ from: LID, identity: "12" }), state(), NOW), { action: "ignore", reason: "identity_unverified" });
  assert.deepEqual(classifyInbound(live({ from: LID, identity: "abc573" }), state(), NOW), { action: "ignore", reason: "identity_unverified" });
});

test("un chat @c.us toma su identidad del propio chat, no de un dato declarado", () => {
  const d = classifyInbound(live({ from: "573153863179@c.us", identity: "573009998877" }), state(), NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") assert.equal(d.identity, "573153863179");
});

test("mensajes propios, grupos, estados, difusiones y canales se descartan", () => {
  const cases: Array<[Partial<InboundFacts>, string]> = [
    [{ fromMe: true }, "own_message"],
    [{ from: "120363000000000000@g.us" }, "group"],
    [{ from: "status@broadcast" }, "status_or_broadcast"],
    [{ isStatus: true }, "status_or_broadcast"],
    [{ broadcast: true }, "status_or_broadcast"],
    [{ from: "120363000000000000@newsletter" }, "newsletter"],
    [{ from: "573001112233@s.whatsapp.net" }, "unsupported_address"],
  ];
  for (const [over, reason] of cases) {
    const d = classifyInbound(live(over), state(), NOW);
    assert.deepEqual(d, { action: "ignore", reason }, JSON.stringify(over));
  }
});

test("contenido que no es texto o texto vacío no se reenvía", () => {
  for (const over of [{ type: "image" }, { type: "ptt" }, { body: "" }, { body: "   " }, { body: null }]) {
    assert.deepEqual(classifyInbound(live(over), state(), NOW), { action: "ignore", reason: "not_text" }, JSON.stringify(over));
  }
});

test("mensajes antiguos, con marca futura o sin identificador se descartan", () => {
  assert.deepEqual(classifyInbound(live({ timestampSec: NOW - 121 }), state(), NOW), { action: "ignore", reason: "too_old" });
  assert.deepEqual(classifyInbound(live({ timestampSec: NOW + 61 }), state(), NOW), { action: "ignore", reason: "future_timestamp" });
  assert.deepEqual(classifyInbound(live({ id: "" }), state(), NOW), { action: "ignore", reason: "missing_fields" });
  assert.deepEqual(classifyInbound(live({ timestampSec: null }), state(), NOW), { action: "ignore", reason: "missing_fields" });
  // Límite exacto: 120 s de antigüedad todavía se acepta.
  assert.equal(classifyInbound(live({ timestampSec: NOW - 120 }), state(), NOW).action, "forward");
});

test("un duplicado del mismo identificador no se reenvía dos veces", () => {
  const st = state();
  assert.equal(classifyInbound(live(), st, NOW).action, "forward");
  assert.deepEqual(classifyInbound(live(), st, NOW), { action: "ignore", reason: "duplicate" });
});

test("un contacto guardado o que ya escribió antes no se ignora por ese motivo", () => {
  // La política no consulta agenda ni historial: cualquier chat individual nuevo es elegible.
  const st = state();
  assert.equal(classifyInbound(live({ id: "A1" }), st, NOW).action, "forward");
  assert.equal(classifyInbound(live({ id: "A2", body: "segunda" }), st, NOW).action, "forward");
});

test("el conjunto de identificadores vistos está acotado", () => {
  const s = new BoundedSet(2);
  s.add("a");
  s.add("b");
  s.add("c");
  assert.equal(s.has("a"), false);
  assert.equal(s.has("c"), true);
});
