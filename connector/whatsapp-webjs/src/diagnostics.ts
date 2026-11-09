// Diagnóstico de solo lectura del arranque de whatsapp-web.js. Nunca extrae datos personales: solo booleanos,
// versiones y tipos de error. Todo texto que llega a un registro pasa por sanitize().
import { createRequire } from "node:module";

const requireFromHere = createRequire(import.meta.url);

// Quita lo que identifica a personas o sesiones: rutas de usuario (nombre de cuenta), JIDs/identificadores (con @),
// secuencias largas de dígitos (números de teléfono, códigos) y saltos de línea. Acota la longitud.
export function sanitize(text: unknown, max = 160): string {
  const raw = typeof text === "string" ? text : String(text ?? "");
  return raw
    .replace(/[A-Za-z]:\\[^\s"']*/g, "<ruta>")
    .replace(/\/(?:home|Users)\/[^\s"']*/g, "<ruta>")
    .replace(/\S+@\S+/g, "<id>")
    .replace(/\d{6,}/g, "<n>")
    .replace(/[\r\n\t]+/g, " ")
    .slice(0, max);
}

// Resumen seguro de un error: nombre y mensaje saneado (los errores lanzados como cadena se tratan igual).
export function errorSummary(err: unknown): string {
  if (err instanceof Error) return `name=${err.name} msg="${sanitize(err.message)}"`;
  return `name=${typeof err === "string" ? "thrown_string" : "unknown"} msg="${sanitize(err)}"`;
}

// Versión de la librería instalada (no depende de la sesión).
export function wwebjsVersion(): string {
  try {
    return String((requireFromHere("whatsapp-web.js/package.json") as { version?: string }).version ?? "unknown");
  } catch {
    return "unknown";
  }
}

// Sonda de solo lectura que se ejecuta DENTRO de la página de WhatsApp Web (se serializa con toString).
// Debe ser autocontenida: no puede usar nada del módulo. Solo devuelve booleanos, una versión y un estado de DOM.
export function pageProbe(): Record<string, unknown> {
  const w = window as any;
  const hasModule = (name: string): boolean => {
    try {
      return typeof w.require === "function" && Boolean(w.require(name));
    } catch {
      return false;
    }
  };
  return {
    wwebjs_injected: typeof w.WWebJS !== "undefined",
    auth_store: typeof w.AuthStore !== "undefined",
    require_available: typeof w.require === "function",
    module_socket: hasModule("WAWebSocketModel"),
    module_cmd: hasModule("WAWebCmd"),
    module_conn: hasModule("WAWebConnModel"),
    module_user_prefs: hasModule("WAWebUserPrefsMeUser"),
    wa_version: typeof w.Debug?.VERSION === "string" ? w.Debug.VERSION : null,
    dom_ready_state: document.readyState,
  };
}
