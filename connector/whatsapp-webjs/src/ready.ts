// Compuerta de "ready". "authenticated" NO significa operativo: whatsapp-web.js emite "ready" solo cuando termina
// de inyectar sus módulos en WhatsApp Web. Esta compuerta vigila ese paso con un plazo explícito: si "ready" no
// llega, el conector falla con un error claro en lugar de quedarse indefinidamente en "authenticated".
export type ReadyPhase = "idle" | "waiting_ready" | "ready" | "timed_out";

export class ReadyGate {
  private phase: ReadyPhase = "idle";
  private since = 0;

  constructor(private readonly timeoutMs: number) {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error("plazo de ready no válido");
  }

  // Primera autenticación: abre el plazo. Repeticiones de "authenticated" no lo reinician.
  authenticated(nowMs: number): void {
    if (this.phase === "idle") {
      this.phase = "waiting_ready";
      this.since = nowMs;
    }
  }

  // Solo se llama cuando el estado real de la sesión es CONNECTED. Un timeout ya declarado es definitivo.
  ready(): void {
    if (this.phase !== "timed_out") this.phase = "ready";
  }

  // Avanza el reloj. Devuelve la fase vigente; pasa a timed_out solo si el plazo ha vencido sin ready.
  check(nowMs: number): ReadyPhase {
    if (this.phase === "waiting_ready" && nowMs - this.since >= this.timeoutMs) this.phase = "timed_out";
    return this.phase;
  }

  elapsedMs(nowMs: number): number {
    return this.phase === "waiting_ready" || this.phase === "timed_out" ? Math.max(0, nowMs - this.since) : 0;
  }

  get current(): ReadyPhase {
    return this.phase;
  }
}
