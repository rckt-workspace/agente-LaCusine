// Identidad de chat para whatsapp-web.js. Nunca se adivina un número a partir de una dirección @lid.
//
//  * Chat individual con número visible (digitos@c.us): el identificador que viaja al backend son los dígitos.
//    Para responder se reconstruye digitos@c.us, que es el mismo chat.
//  * Chat con dirección @lid: el identificador que viaja al backend es OPACO (12 dígitos que empiezan por 9,
//    derivados con HMAC de una clave local). La dirección @lid original se guarda en state/ para responder al
//    mismo chat. No es un número de teléfono.
//  * Cualquier otra dirección no se encamina. Un identificador no resoluble nunca se envía a ciegas.
import { createHmac, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export class UnresolvedRouteError extends Error {
  constructor() {
    super("unresolved_route");
    this.name = "UnresolvedRouteError";
  }
}

const OPAQUE_PATTERN = /^9[0-9]{11}$/;
const PHONE_PATTERN = /^[0-9]{6,20}$/;
const C_US = "@c.us";

export class RouteTable {
  private readonly toChat = new Map<string, string>();
  private readonly toRoute = new Map<string, string>();
  // Ruta opaca -> número telefónico verificado por WhatsApp (solo para rutas @lid).
  private readonly identities = new Map<string, string>();

  // Rutas que han sido reenviadas al backend desde un evento entrante auténtico (base de la atención pública).
  private readonly known = new Set<string>();

  constructor(
    private readonly key: Buffer,
    private readonly file?: string,
    private readonly identityFile?: string,
    private readonly knownFile?: string,
  ) {
    if (knownFile && existsSync(knownFile)) {
      try {
        for (const route of JSON.parse(readFileSync(knownFile, "utf8")) as string[]) {
          if (typeof route === "string" && (PHONE_PATTERN.test(route) || OPAQUE_PATTERN.test(route))) this.known.add(route);
        }
      } catch {
        // Archivo dañado: las rutas no conocidas quedan fuera de la atención pública (falla cerrado).
      }
    }
    if (identityFile && existsSync(identityFile)) {
      try {
        const entries = JSON.parse(readFileSync(identityFile, "utf8")) as Array<[string, string]>;
        for (const [route, phone] of entries) {
          if (OPAQUE_PATTERN.test(route) && PHONE_PATTERN.test(phone)) this.identities.set(route, phone);
        }
      } catch {
        // Archivo dañado: las rutas @lid sin identidad quedan sin autorizar (falla cerrado).
      }
    }
    if (file && existsSync(file)) {
      try {
        const entries = JSON.parse(readFileSync(file, "utf8")) as Array<[string, string]>;
        for (const [route, chatId] of entries) {
          if (OPAQUE_PATTERN.test(route) && chatId.endsWith("@lid")) {
            this.toChat.set(route, chatId);
            this.toRoute.set(chatId, route);
          }
        }
      } catch {
        // Archivo dañado: nunca se envía a ciegas; los chats @lid sin correspondencia quedan sin resolver.
      }
    }
  }

  // Identificador del chat para el backend. null = dirección no soportada.
  routeFor(chatId: string): string | null {
    if (chatId.endsWith(C_US)) {
      const digits = chatId.slice(0, -C_US.length);
      return PHONE_PATTERN.test(digits) ? digits : null;
    }
    if (!chatId.endsWith("@lid")) return null;
    const known = this.toRoute.get(chatId);
    if (known) return known;
    let counter = 0;
    let candidate: string;
    do {
      const material = counter === 0 ? chatId : chatId + "#" + String(counter);
      const digest = createHmac("sha256", this.key).update(material).digest();
      candidate = "9" + (BigInt("0x" + digest.subarray(0, 8).toString("hex")) % 10n ** 11n).toString().padStart(11, "0");
      counter += 1;
    } while (this.toChat.has(candidate));
    this.toChat.set(candidate, chatId);
    this.toRoute.set(chatId, candidate);
    this.persist();
    return candidate;
  }

  // Dirección de envío para un identificador recibido del backend. null = no resoluble con certeza.
  jidFor(route: string): string | null {
    if (OPAQUE_PATTERN.test(route)) return this.toChat.get(route) ?? null;
    if (PHONE_PATTERN.test(route)) return route + C_US;
    return null;
  }

  // ¿La ruta nació de un evento entrante reenviado? Único criterio para responder en atención pública.
  knownRoute(route: string): boolean {
    return this.known.has(route);
  }

  // Marca una ruta como originada por un mensaje entrante reenviado. Idempotente y persistente.
  markKnown(route: string): void {
    if (this.known.has(route) || !(PHONE_PATTERN.test(route) || OPAQUE_PATTERN.test(route))) return;
    this.known.add(route);
    if (this.knownFile) {
      writeFileSync(this.knownFile, JSON.stringify([...this.known]), { encoding: "utf8", mode: 0o600 });
    }
  }

  // Número telefónico verificado de una ruta. Para @c.us es el propio chat; para @lid, solo si se registró.
  identityFor(route: string): string | null {
    // Primero la ruta opaca: un identificador de 12 dígitos que empieza por 9 también "parece" un número.
    if (OPAQUE_PATTERN.test(route)) return this.identities.get(route) ?? null;
    if (PHONE_PATTERN.test(route)) return route;
    return null;
  }

  // Registra la identidad verificada de una ruta @lid. Nunca sobrescribe una identidad distinta ya registrada.
  recordIdentity(route: string, phone: string): boolean {
    if (!OPAQUE_PATTERN.test(route) || !PHONE_PATTERN.test(phone)) return false;
    const known = this.identities.get(route);
    if (known !== undefined && known !== phone) return false;
    if (known === phone) return true;
    this.identities.set(route, phone);
    this.persistIdentities();
    return true;
  }

  // Retira la marca de una ruta que el backend NO admitió (rechazada, en conflicto o fallida). Solo se usa cuando
  // la ruta no era conocida antes. Nunca afecta a una ruta con mensajes admitidos previos.
  forgetKnown(route: string): void {
    if (!this.known.delete(route)) return;
    if (this.knownFile) {
      writeFileSync(this.knownFile, JSON.stringify([...this.known]), { encoding: "utf8", mode: 0o600 });
    }
  }

  private persist(): void {
    if (!this.file) return;
    writeFileSync(this.file, JSON.stringify([...this.toChat.entries()]), { encoding: "utf8", mode: 0o600 });
  }

  private persistIdentities(): void {
    if (!this.identityFile) return;
    writeFileSync(this.identityFile, JSON.stringify([...this.identities.entries()]), { encoding: "utf8", mode: 0o600 });
  }
}

export function routeTableFor(stateDir: string): RouteTable {
  mkdirSync(stateDir, { recursive: true });
  const keyPath = join(stateDir, "route-key-webjs.bin");
  let key: Buffer;
  if (existsSync(keyPath)) {
    key = readFileSync(keyPath);
  } else {
    key = randomBytes(32);
    writeFileSync(keyPath, key, { mode: 0o600 });
  }
  return new RouteTable(
    key,
    join(stateDir, "routes-webjs.json"),
    join(stateDir, "identities-webjs.json"),
    join(stateDir, "known-routes-webjs.json"),
  );
}
