import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { RouteTable, routeTableFor } from "../routes.js";

const LID = "abc123def456@lid";
const LID_2 = "zz999@lid";
const KEY = Buffer.alloc(32, 9);

test("phone-visible chats route to the number itself and resolve back to the individual address", () => {
  const t = new RouteTable(KEY);
  assert.equal(t.routeFor("573001112233@s.whatsapp.net"), "573001112233");
  assert.equal(t.jidFor("573001112233"), "573001112233@s.whatsapp.net");
});

test("@lid chats get a stable opaque 12-digit route that is NOT a Colombian phone number", () => {
  const t = new RouteTable(KEY);
  const route = t.routeFor(LID);
  assert.ok(route);
  assert.match(route as string, /^9[0-9]{11}$/);
  assert.equal(t.routeFor(LID), route, "same chat, same route");
  assert.notEqual(t.routeFor(LID_2), route, "different chats, different routes");
});

test("the opaque route resolves back to the exact @lid chat for the reply", () => {
  const t = new RouteTable(KEY);
  const route = t.routeFor(LID) as string;
  assert.equal(t.jidFor(route), LID);
});

test("an unknown opaque route is never resolved: the reply is refused instead of guessed", () => {
  const t = new RouteTable(KEY);
  assert.equal(t.jidFor("912345678901"), null);
  assert.equal(t.jidFor("not-a-route"), null);
});

test("a different local key yields a different route for the same chat", () => {
  assert.notEqual(new RouteTable(Buffer.alloc(32, 1)).routeFor(LID), new RouteTable(Buffer.alloc(32, 2)).routeFor(LID));
});

test("routes survive a restart through the state file, so a reply after restart still reaches the chat", () => {
  const dir = mkdtempSync(join(tmpdir(), "lc-routes-"));
  const first = routeTableFor(dir);
  const route = first.routeFor(LID) as string;
  const second = routeTableFor(dir);
  assert.equal(second.jidFor(route), LID);
  assert.equal(second.routeFor(LID), route);
});

test("a corrupt routes file is ignored rather than trusted", () => {
  const dir = mkdtempSync(join(tmpdir(), "lc-routes-"));
  routeTableFor(dir).routeFor(LID);
  // Un archivo no válido no debe impedir arrancar ni resolver destinos desde otra fuente.
  const t = new RouteTable(KEY, join(dir, "does-not-exist.json"));
  assert.equal(t.jidFor("912345678901"), null);
});
