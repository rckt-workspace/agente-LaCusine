// Cerrojo de instancia única para el modo de observación. Evita dos conectores con la misma sesión.
// Un cerrojo cuyo proceso ya no existe se considera obsoleto y se reemplaza.
import { closeSync, existsSync, openSync, readFileSync, unlinkSync, writeSync } from "node:fs";

export function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM: el proceso existe pero pertenece a otro usuario.
    return (err as NodeJS.ErrnoException)?.code === "EPERM";
  }
}

export type LockResult = { ok: true } | { ok: false; reason: "lock_held" };

export function acquireLock(path: string, pid: number = process.pid, alive: (p: number) => boolean = processAlive): LockResult {
  if (existsSync(path)) {
    const holder = Number(readFileSync(path, "utf8").trim());
    if (Number.isInteger(holder) && holder > 0 && holder !== pid && alive(holder)) {
      return { ok: false, reason: "lock_held" };
    }
    try {
      unlinkSync(path);
    } catch {
      return { ok: false, reason: "lock_held" };
    }
  }
  try {
    const fd = openSync(path, "wx");
    writeSync(fd, String(pid));
    closeSync(fd);
    return { ok: true };
  } catch {
    // Otro proceso lo creó en el mismo instante.
    return { ok: false, reason: "lock_held" };
  }
}

export function releaseLock(path: string, pid: number = process.pid): void {
  try {
    if (existsSync(path) && Number(readFileSync(path, "utf8").trim()) === pid) unlinkSync(path);
  } catch {
    // Nada que hacer: el siguiente arranque reemplazará un cerrojo obsoleto.
  }
}
