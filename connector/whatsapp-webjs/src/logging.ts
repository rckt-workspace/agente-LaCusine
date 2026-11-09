// Registro del conector. UN ÚNICO ESCRITOR por archivo: este proceso abre el archivo de log una vez, de forma
// síncrona, y nadie más escribe en él. La consola recibe solo líneas informativas (say), nunca el registro completo.
// El código QR NUNCA pasa por este módulo (ver qr.ts).
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { pino } from "pino";

export type Logger = ReturnType<typeof createLogger>;

export function createLogger(file: string) {
  mkdirSync(dirname(file), { recursive: true });
  return pino({ level: "info" }, pino.destination({ dest: file, sync: true, mkdir: true }));
}

// Línea informativa para la consola. No contiene datos personales ni códigos de vinculación.
export function say(line: string): void {
  process.stdout.write(line.endsWith("\n") ? line : line + "\n");
}
