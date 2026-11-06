// Pruebas de formato del panel comercial. Ejecutar con: npm test (node:test, sin dependencias nuevas).
import { test } from "node:test";
import assert from "node:assert/strict";
import { cop, pct, usd } from "../src/format.ts";

test("un importe numérico se muestra con cuatro decimales", () => {
  assert.equal(usd(0.0031), "0.0031 USD");
  assert.equal(usd(0), "0.0000 USD");
});

test("un importe que llega como texto no lanza excepción y se marca como sin datos", () => {
  // Regresión: la API devolvía "0.00" como cadena y React quedaba en blanco al llamar toFixed.
  assert.doesNotThrow(() => usd("0.00"));
  assert.equal(usd("0.00"), "Sin datos");
  assert.equal(cop("9870000"), "Sin datos");
  assert.equal(pct("91.7"), "Sin datos");
});

test("valores no finitos y ausentes se tratan como sin datos", () => {
  for (const value of [null, undefined, NaN, Infinity, -Infinity, {}, [], true]) {
    assert.equal(usd(value), "Sin datos");
    assert.equal(cop(value), "Sin datos");
    assert.equal(pct(value), "Sin datos");
  }
});

test("pesos colombianos y porcentajes numéricos", () => {
  assert.equal(cop(9870000), "$9.870.000 COP");
  assert.equal(pct(91.7), "91.7%");
});
