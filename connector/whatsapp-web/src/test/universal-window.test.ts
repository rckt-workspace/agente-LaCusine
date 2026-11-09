import { strict as assert } from "node:assert";
import { test } from "node:test";
import { INBOUND_ALL_MAX_SECONDS, inboundWindowOpen, loadConfig } from "../config.js";
import { classifyInbound, type PolicyState, type RawInbound } from "../policy.js";
import { RouteTable } from "../routes.js";

const NOW = 1_800_000_000;
const SECRET = "s3cr3t-de-prueba-de-32-caracteres!!";
const EXISTING = "573007778899";
const STRANGER = "573009990011";
const base = { WEB_BRIDGE_SECRET: SECRET };

function raw(phone: string, overrides: Partial<RawInbound> = {}): RawInbound {
  return {
    upsertType: "notify",
    fromMe: false,
    remoteJid: `${phone}@s.whatsapp.net`,
    messageId: `ID-${phone}-${Math.random()}`,
    timestampSec: NOW - 5,
    kind: "text",
    text: "hola",
    ...overrides,
  };
}

function universalPolicy(): PolicyState {
  const routes = new RouteTable(Buffer.alloc(32, 3));
  return {
    knownJids: new Set([`${EXISTING}@s.whatsapp.net`]),
    seenIds: new Set(),
    protectKnownContacts: false,
    routeFor: (jid) => routes.routeFor(jid),
  };
}

test("universal mode: a new text from an unknown individual chat is forwarded", () => {
  assert.equal(classifyInbound(raw(STRANGER), universalPolicy(), NOW).action, "forward");
});

test("universal mode: an existing contact with a new message is forwarded", () => {
  assert.equal(classifyInbound(raw(EXISTING, { text: "¿tienen cafeteras?" }), universalPolicy(), NOW).action, "forward");
});

test("universal mode: groups, history, own messages, duplicates and old messages are still refused", () => {
  const p = universalPolicy();
  assert.deepEqual(classifyInbound(raw(STRANGER, { remoteJid: "120363000000000000@g.us" }), p, NOW), { action: "ignore", reason: "group" });
  assert.deepEqual(classifyInbound(raw(STRANGER, { upsertType: "append", timestampSec: NOW - 3600 }), p, NOW),
    { action: "ignore", reason: "history_sync" });
  assert.deepEqual(classifyInbound(raw(STRANGER, { fromMe: true }), p, NOW), { action: "ignore", reason: "own_message" });
  assert.deepEqual(classifyInbound(raw(STRANGER, { timestampSec: NOW - 3600 }), p, NOW), { action: "ignore", reason: "too_old" });
  const once = raw(STRANGER, { messageId: "DUP-1" });
  assert.equal(classifyInbound(once, p, NOW).action, "forward");
  assert.deepEqual(classifyInbound(once, p, NOW), { action: "ignore", reason: "duplicate" });
});

test("window: open before expiry, closed at and after expiry", () => {
  const cfg = loadConfig({ ...base, WEB_BRIDGE_INBOUND_MODE: "web_inbound_all", WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 900) }, NOW);
  assert.equal(inboundWindowOpen(cfg, NOW + 899), true);
  assert.equal(inboundWindowOpen(cfg, NOW + 900), false);
  assert.equal(inboundWindowOpen(cfg, NOW + 5000), false);
});

test("window: single_chat mode never closes the window by itself", () => {
  const cfg = loadConfig(base, NOW);
  assert.equal(cfg.inboundMode, "single_chat");
  assert.equal(inboundWindowOpen(cfg, NOW + 10_000_000), true);
});

test("window: universal mode requires an explicit future expiry", () => {
  assert.throws(() => loadConfig({ ...base, WEB_BRIDGE_INBOUND_MODE: "web_inbound_all" }, NOW), /INBOUND_UNTIL/);
  assert.throws(() => loadConfig({ ...base, WEB_BRIDGE_INBOUND_MODE: "web_inbound_all", WEB_BRIDGE_INBOUND_UNTIL: String(NOW - 1) }, NOW),
    /INBOUND_UNTIL/);
});

test("window: universal mode is capped at 15 minutes", () => {
  assert.equal(INBOUND_ALL_MAX_SECONDS, 900);
  assert.throws(() => loadConfig({ ...base, WEB_BRIDGE_INBOUND_MODE: "web_inbound_all",
    WEB_BRIDGE_INBOUND_UNTIL: String(NOW + 901) }, NOW), /15 minutos/);
});

test("window: an unknown mode is refused at startup", () => {
  assert.throws(() => loadConfig({ ...base, WEB_BRIDGE_INBOUND_MODE: "publico" }, NOW), /INBOUND_MODE no válido/);
});
