// Formato de cifras del panel comercial. Puro (sin React) para poder probarse.
//
// Contrato: la API devuelve importes como números JSON. Si llega cualquier otro tipo (texto, null, NaN, Infinity)
// se muestra "Sin datos" en lugar de lanzar una excepción: la pantalla nunca debe quedar en blanco por un dato.

export const SIN_DATOS = "Sin datos";

export function asFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

export function pct(value: unknown): string {
  const n = asFiniteNumber(value);
  return n === null ? SIN_DATOS : `${n}%`;
}

export function cop(value: unknown): string {
  const n = asFiniteNumber(value);
  return n === null ? SIN_DATOS : `$${n.toLocaleString("es-CO")} COP`;
}

export function usd(value: unknown): string {
  const n = asFiniteNumber(value);
  return n === null ? SIN_DATOS : `${n.toFixed(4)} USD`;
}
