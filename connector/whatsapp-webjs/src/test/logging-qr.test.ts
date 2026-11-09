import { strict as assert } from "node:assert";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createLogger } from "../logging.js";
import { printQr, renderQr } from "../qr.js";

const SAMPLE_QR = "2@FAKE-QR-PARA-PRUEBA-NO-REAL,abc123,def456,ghi789==";

test("el QR se dibuja como arte de bloques completo, sin el texto del código", async () => {
  const art = await renderQr(SAMPLE_QR);
  const lines = art.split("\n").filter((l) => l.trim().length > 0);
  assert.ok(lines.length >= 10, "un QR legible ocupa varias líneas");
  assert.ok(/[█▀▄]/u.test(art), "usa caracteres de bloque UTF-8");
  assert.ok(!art.includes(SAMPLE_QR), "el texto del código no aparece en claro");
});

test("el QR se escribe solo en la consola; el registro no lo recibe", async () => {
  const dir = mkdtempSync(join(tmpdir(), "webjs-qr-"));
  const file = join(dir, "connector.log");
  const log = createLogger(file);
  const consoleOut: string[] = [];
  await printQr(SAMPLE_QR, (t) => consoleOut.push(t));
  log.info("stage=connection state=qr_required");
  const written = consoleOut.join("");
  assert.ok(/[█▀▄]/u.test(written), "la consola recibe el QR");
  const onDisk = readFileSync(file, "utf8");
  assert.ok(!onDisk.includes(SAMPLE_QR), "el archivo de log no contiene el código");
  assert.ok(!/[█▀▄]/u.test(onDisk), "el archivo de log no contiene arte de QR");
  assert.ok(onDisk.includes("stage=connection state=qr_required"));
});

test("registro: un único escritor síncrono por archivo, UTF-8 y líneas JSON completas", () => {
  const dir = mkdtempSync(join(tmpdir(), "webjs-log-"));
  const file = join(dir, "connector.log");
  const log = createLogger(file);
  log.info("stage=00_message_received kind=individual state=ready cid=abcdef0123");
  log.info("stage=01_classified cid=abcdef0123 decision=ignore reason=stop_active");
  const lines = readFileSync(file, "utf8").trim().split("\n");
  assert.equal(lines.length, 2);
  for (const line of lines) {
    const parsed = JSON.parse(line) as { msg: string; level: number };
    assert.equal(typeof parsed.msg, "string");
    assert.equal(parsed.level, 30);
  }
});

test("registro: los textos con acentos se conservan en UTF-8", () => {
  const dir = mkdtempSync(join(tmpdir(), "webjs-utf8-"));
  const file = join(dir, "connector.log");
  createLogger(file).info("stage=connection state=ready mensaje=vinculación ñandú");
  assert.ok(readFileSync(file, "utf8").includes("vinculación ñandú"));
});
