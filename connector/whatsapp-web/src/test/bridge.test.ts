import { strict as assert } from "node:assert";
import type { AddressInfo } from "node:net";
import { test } from "node:test";
import { createBridgeServer, type BridgeDeps } from "../bridge.js";
import { sign, verify } from "../hmac.js";

const SECRET = "s3cr3t-de-prueba-de-32-caracteres!!";
const NOW = 1_800_000_000;
const SEND_BODY = JSON.stringify({ to: "573001112233", text: "hola", idempotency_key: "k1" });
// Vector de referencia calculado con app/integrations/web_bridge.py. Debe coincidir byte a byte.
const PYTHON_VECTOR = "3df9a7b82f064dc8ead2502faf2f6e09d6834a97e48a0cb9df3bbafcecd29f82";

interface Harness {
  url: string;
  close: () => Promise<void>;
  sent: Array<[string, string]>;
  state: { enabled: boolean; killed: boolean; failNext: boolean; kills: number; unlinks: number };
}

async function start(overrides: Partial<BridgeDeps> = {}): Promise<Harness> {
  const state = { enabled: false, killed: false, failNext: false, kills: 0, unlinks: 0 };
  const sent: Array<[string, string]> = [];
  const server = createBridgeServer({
    secret: SECRET,
    isEnabled: () => state.enabled,
    isKilled: () => state.killed,
    connectionState: () => "open",
    sendText: async (phone, text) => {
      if (state.failNext) {
        state.failNext = false;
        throw new Error("boom");
      }
      sent.push([phone, text]);
      return `wamid.TEST${sent.length}`;
    },
    onKill: async () => {
      state.kills += 1;
    },
    onUnlink: async () => {
      state.unlinks += 1;
    },
    nowSec: () => NOW,
    ...overrides,
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
    sent,
    state,
  };
}

function signedHeaders(direction: string, body: string) {
  const stamp = String(NOW);
  return {
    "Content-Type": "application/json",
    "X-Bridge-Timestamp": stamp,
    "X-Bridge-Signature": sign(SECRET, direction, stamp, body),
  };
}

async function post(url: string, path: string, body: string, direction: string, headers?: Record<string, string>) {
  return fetch(`${url}${path}`, { method: "POST", headers: headers ?? signedHeaders(direction, body), body });
}

test("the TypeScript signer produces the same HMAC as the Python backend", () => {
  assert.equal(sign(SECRET, "send", String(NOW), SEND_BODY), PYTHON_VECTOR);
  assert.equal(verify(SECRET, "send", String(NOW), SEND_BODY, PYTHON_VECTOR, NOW), true);
});

test("signatures are bound to direction, time window and body", () => {
  const sig = sign(SECRET, "send", String(NOW), SEND_BODY);
  assert.equal(verify(SECRET, "inbound", String(NOW), SEND_BODY, sig, NOW), false);
  assert.equal(verify(SECRET, "send", String(NOW - 301), SEND_BODY, sig, NOW), false);
  assert.equal(verify(SECRET, "send", String(NOW), SEND_BODY + " ", sig, NOW), false);
  assert.equal(verify(SECRET, "send", String(NOW), SEND_BODY, undefined, NOW), false);
});

test("unsigned or wrongly signed requests are rejected before anything else", async () => {
  const h = await start();
  try {
    const unsigned = await fetch(`${h.url}/send`, { method: "POST", body: SEND_BODY });
    assert.equal(unsigned.status, 401);
    const wrong = await post(h.url, "/send", SEND_BODY, "send", { ...signedHeaders("send", SEND_BODY), "X-Bridge-Signature": "00" });
    assert.equal(wrong.status, 401);
    assert.equal(h.sent.length, 0);
  } finally {
    await h.close();
  }
});

test("sending is refused while automatic replies are not authorized, and nothing reaches WhatsApp", async () => {
  const h = await start();
  try {
    const response = await post(h.url, "/send", SEND_BODY, "send");
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), { error: "send_disabled" });
    assert.equal(h.sent.length, 0);
  } finally {
    await h.close();
  }
});

test("an authorized send goes out once; a repeated idempotency key never sends twice", async () => {
  const h = await start();
  try {
    h.state.enabled = true;
    const first = await post(h.url, "/send", SEND_BODY, "send");
    assert.equal(first.status, 200);
    assert.deepEqual(await first.json(), { message_id: "wamid.TEST1" });
    const second = await post(h.url, "/send", SEND_BODY, "send");
    assert.equal(second.status, 200);
    assert.deepEqual(await second.json(), { message_id: "wamid.TEST1", duplicate: true });
    assert.deepEqual(h.sent, [["573001112233", "hola"]]);
  } finally {
    await h.close();
  }
});

test("a kill switch blocks sends immediately even when replies are authorized", async () => {
  const h = await start();
  try {
    h.state.enabled = true;
    h.state.killed = true;
    const response = await post(h.url, "/send", SEND_BODY, "send");
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), { error: "killed" });
    assert.equal(h.sent.length, 0);
  } finally {
    await h.close();
  }
});

test("invalid recipients and empty texts are rejected without sending", async () => {
  const h = await start();
  try {
    h.state.enabled = true;
    const badTo = JSON.stringify({ to: "not-a-number", text: "hola", idempotency_key: "k2" });
    assert.equal((await post(h.url, "/send", badTo, "send")).status, 400);
    const emptyText = JSON.stringify({ to: "573001112233", text: "", idempotency_key: "k3" });
    assert.equal((await post(h.url, "/send", emptyText, "send")).status, 400);
    assert.equal(h.sent.length, 0);
  } finally {
    await h.close();
  }
});

test("a send failure is reported as an error without leaking the cause", async () => {
  const h = await start();
  try {
    h.state.enabled = true;
    h.state.failNext = true;
    const response = await post(h.url, "/send", SEND_BODY, "send");
    assert.equal(response.status, 500);
    const text = await response.text();
    assert.equal(text.includes("boom"), false);
  } finally {
    await h.close();
  }
});

test("status exposes only connection state and modes, never personal data", async () => {
  const h = await start();
  try {
    const response = await fetch(`${h.url}/status`, { headers: signedHeaders("status", "") });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { connection: "open", auto_reply: false, killed: false, inbound_until: null });
  } finally {
    await h.close();
  }
});

test("status reports the window expiry so the launcher can verify the connector got the same value", async () => {
  const h = await start({ inboundUntil: () => NOW + 900 });
  try {
    const response = await fetch(`${h.url}/status`, { headers: signedHeaders("status", "") });
    assert.equal((await response.json()).inbound_until, NOW + 900);
  } finally {
    await h.close();
  }
});

test("kill and unlink are signed control operations and call the socket handlers", async () => {
  const h = await start();
  try {
    const unsigned = await fetch(`${h.url}/kill`, { method: "POST" });
    assert.equal(unsigned.status, 401);
    assert.equal(h.state.kills, 0);
    assert.equal((await post(h.url, "/kill", "", "control")).status, 200);
    assert.equal(h.state.kills, 1);
    assert.equal((await post(h.url, "/unlink", "", "control")).status, 200);
    assert.equal(h.state.unlinks, 1);
  } finally {
    await h.close();
  }
});

test("unknown routes and wrong methods return 404", async () => {
  const h = await start();
  try {
    assert.equal((await fetch(`${h.url}/nope`)).status, 404);
    assert.equal((await fetch(`${h.url}/send`)).status, 404);
  } finally {
    await h.close();
  }
});

test("an unresolvable destination is refused with 422 (nothing was sent, so no retry is needed)", async () => {
  const { UnresolvedRouteError } = await import("../routes.js");
  const h = await start({
    sendText: async () => {
      throw new UnresolvedRouteError();
    },
  });
  try {
    h.state.enabled = true;
    const response = await post(h.url, "/send", SEND_BODY, "send");
    assert.equal(response.status, 422);
    assert.deepEqual(await response.json(), { error: "unresolved_route" });
    assert.equal(h.sent.length, 0);
  } finally {
    await h.close();
  }
});
