// Tabla de rutas del conector. Traduce el identificador que viaja al backend al chat real de WhatsApp.
//
//  * Contacto con número visible (@s.whatsapp.net): el identificador es el propio número. Sin tabla.
//  * Contacto con dirección @lid (sin número visible en Baileys 6.7.24): se asigna un identificador OPACO de
//    12 dígitos, derivado de HMAC(clave local, dirección). NO es un número de teléfono y no se inventa ninguno.
//    La correspondencia se guarda solo en state/ (ignorado por Git) para resolver la respuesta.
//
// Un identificador que no se puede resolver con certeza NO se envía: el envío se rechaza con "unresolved_route".
import { createHmac, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// Destino no resoluble: el mensaje nunca sale. Se distingue de un fallo de envío (que sería incierto).
export class UnresolvedRouteError extends Error {
  constructor() {
    super("unresolved_route");
    this.name = "UnresolvedRouteError";
  }
}

const OPAQUE_PATTERN = /^9[0-9]{11}$/;
const PHONE_PATTERN = /^[0-9]{6,20}$/;
const INDIVIDUAL_SUFFIX = "@s.whatsapp.net";

export class RouteTable {
  private readonly toJid = new Map<string, string>();
  private readonly toRoute = new Map<string, string>();

  constructor(private readonly key: Buffer, private readonly file?: string) {
    if (file && existsSync(file)) {
      try {
        const entries = JSON.parse(readFileSync(file, "utf8")) as Array<[string, string]>;
        for (const [route, jid] of entries) {
          if (OPAQUE_PATTERN.test(route)) {
            this.toJid.set(route, jid);
            this.toRoute.set(jid, route);
          }
        }
      } catch {
        // Archivo dañado: se ignora y se reconstruye al recibir mensajes. Nunca se envía a ciegas.
      }
    }
  }

  // Identificador del chat para el backend. Números visibles: el número. Direcciones @lid: opaco.
  routeFor(jid: string): string | null {
    if (jid.endsWith(INDIVIDUAL_SUFFIX)) {
      const digits = jid.slice(0, -INDIVIDUAL_SUFFIX.length).split(":")[0] ?? "";
      return PHONE_PATTERN.test(digits) ? digits : null;
    }
    if (!jid.endsWith("@lid")) return null;
    const known = this.toRoute.get(jid);
    if (known) return known;
    const digest = createHmac("sha256", this.key).update(jid).digest();
    let candidate = "9" + (BigInt("0x" + digest.subarray(0, 8).toString("hex")) % 10n ** 11n).toString().padStart(11, "0");
    let counter = 0;
    while (this.toJid.has(candidate)) {
      counter += 1;
      candidate = "9" + (BigInt("0x" + createHmac("sha256", this.key).update(`${jid}#${counter}`).digest().subarray(0, 8).toString("hex")) % 10n ** 11n).toString().padStart(11, "0");
    }
    this.toJid.set(candidate, jid);
    this.toRoute.set(jid, candidate);
    this.persist();
    return candidate;
  }

  // Destino de envío para un identificador recibido del backend. null = no resoluble con certeza.
  jidFor(route: string): string | null {
    if (OPAQUE_PATTERN.test(route)) return this.toJid.get(route) ?? null;
    if (PHONE_PATTERN.test(route)) return route + INDIVIDUAL_SUFFIX;
    return null;
  }

  private persist(): void {
    if (!this.file) return;
    writeFileSync(this.file, JSON.stringify([...this.toJid.entries()]), { encoding: "utf8", mode: 0o600 });
  }
}

// Clave local para el HMAC del identificador opaco. Se crea una vez en state/ y no se muestra.
export function loadOrCreateRouteKey(stateDir: string): Buffer {
  mkdirSync(stateDir, { recursive: true });
  const path = join(stateDir, "route-key.bin");
  if (existsSync(path)) return readFileSync(path);
  const key = randomBytes(32);
  writeFileSync(path, key, { mode: 0o600 });
  return key;
}

export function routeTableFor(stateDir: string): RouteTable {
  return new RouteTable(loadOrCreateRouteKey(stateDir), join(stateDir, "routes.json"));
}
