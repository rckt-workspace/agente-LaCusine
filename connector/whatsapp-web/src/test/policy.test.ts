import { strict as assert } from "node:assert";
import { test } from "node:test";
import { classifyInbound, phoneDigitsFromJid, rememberKnownJids, type PolicyState, type RawInbound } from "../policy.js";
import { RouteTable } from "../routes.js";

const NOW = 1_800_000_000;
const CLIENT = "573001112233";
const CLIENT_JID = `${CLIENT}@s.whatsapp.net`;
const LID_JID = "abc123def456@lid";

function table(): RouteTable {
  return new RouteTable(Buffer.alloc(32, 7));
}

function state(opts: { protect?: boolean; known?: string[] } = {}): PolicyState {
  const routes = table();
  return {
    knownJids: new Set(opts.known ?? []),
    seenIds: new Set(),
    protectKnownContacts: opts.protect ?? false,
    routeFor: (jid) => routes.routeFor(jid),
  };
}

function raw(overrides: Partial<RawInbound> = {}): RawInbound {
  return {
    upsertType: "notify",
    fromMe: false,
    remoteJid: CLIENT_JID,
    messageId: `ID-${Math.random()}`,
    timestampSec: NOW - 5,
    kind: "text",
    text: "Hola, busco una cafetera",
    ...overrides,
  };
}

test("a new 1:1 text message from a number is forwarded with that number as route", () => {
  const d = classifyInbound(raw(), state(), NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") assert.equal(d.routeId, CLIENT);
});

test("a message from an @lid address is forwarded with an opaque route, not a phone number", () => {
  const d = classifyInbound(raw({ remoteJid: LID_JID }), state(), NOW);
  assert.equal(d.action, "forward");
  if (d.action === "forward") {
    assert.match(d.routeId, /^9[0-9]{11}$/);
    assert.equal(phoneDigitsFromJid(LID_JID), null);
  }
});

test("universal mode: existing contacts are forwarded like anybody else", () => {
  const d = classifyInbound(raw(), state({ known: [CLIENT_JID], protect: false }), NOW);
  assert.equal(d.action, "forward");
});

test("single_chat mode: existing contacts stay protected", () => {
  assert.deepEqual(classifyInbound(raw(), state({ known: [CLIENT_JID], protect: true }), NOW),
    { action: "ignore", reason: "known_contact" });
});

test("old history (append with an old timestamp) is never processed", () => {
  assert.deepEqual(classifyInbound(raw({ upsertType: "append", timestampSec: NOW - 3600 }), state(), NOW),
    { action: "ignore", reason: "history_sync" });
});

test("a recent append is still never dispatched: there is no reliable signal that it is a new delivery", () => {
  assert.deepEqual(classifyInbound(raw({ upsertType: "append", timestampSec: NOW - 5 }), state(), NOW),
    { action: "ignore", reason: "history_sync" });
});

test("own messages, groups, status, broadcasts and channels are ignored", () => {
  const s = state();
  assert.deepEqual(classifyInbound(raw({ fromMe: true }), s, NOW), { action: "ignore", reason: "own_message" });
  assert.deepEqual(classifyInbound(raw({ remoteJid: "120363000000000000@g.us" }), s, NOW), { action: "ignore", reason: "group" });
  assert.deepEqual(classifyInbound(raw({ remoteJid: "status@broadcast" }), s, NOW), { action: "ignore", reason: "status_or_broadcast" });
  assert.deepEqual(classifyInbound(raw({ remoteJid: "120363000000000001@newsletter" }), s, NOW), { action: "ignore", reason: "newsletter" });
});

test("non-text content and edited messages are ignored, so an edit never produces a second reply", () => {
  assert.deepEqual(classifyInbound(raw({ kind: "other", text: null }), state(), NOW), { action: "ignore", reason: "not_text" });
});

test("missing identifiers, stale messages and unroutable addresses are ignored", () => {
  assert.equal(classifyInbound(raw({ messageId: null }), state(), NOW).action, "ignore");
  assert.deepEqual(classifyInbound(raw({ timestampSec: NOW - 3600 }), state(), NOW), { action: "ignore", reason: "too_old" });
  assert.deepEqual(classifyInbound(raw({ remoteJid: "12@s.whatsapp.net" }), state(), NOW),
    { action: "ignore", reason: "unsupported_address" });
});

test("the same message identifier is forwarded at most once", () => {
  const s = state();
  const m = raw({ messageId: "FIXED-1" });
  assert.equal(classifyInbound(m, s, NOW).action, "forward");
  assert.deepEqual(classifyInbound(m, s, NOW), { action: "ignore", reason: "duplicate" });
});

test("regression: a contact registered with a device suffix is protected without duplicating the suffix", () => {
  const s = state({ protect: true });
  rememberKnownJids(s, ["573001112233:7@s.whatsapp.net"]);
  assert.equal(s.knownJids.has("573001112233@s.whatsapp.net"), true);
  assert.equal(s.knownJids.has("573001112233@s.whatsapp.net@s.whatsapp.net"), false);
  assert.deepEqual(classifyInbound(raw({ remoteJid: "573001112233:12@s.whatsapp.net" }), s, NOW),
    { action: "ignore", reason: "known_contact" });
});

test("rememberKnownJids ignores groups and empty values", () => {
  const s = state();
  assert.equal(rememberKnownJids(s, [CLIENT_JID, "120363@g.us", undefined, null]), 1);
});
