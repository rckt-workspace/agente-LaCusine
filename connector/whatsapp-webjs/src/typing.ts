// Indicador «escribiendo…» por chat. Refleja trabajo real del agente y nunca se queda activo indefinidamente.
//   * start(jid): activa el estado, lo renueva cada refreshMs y lo caduca solo tras maxMs.
//   * stop(jid):  lo desactiva de inmediato (al enviar la respuesta, al fallar o al apagar).
//   * Cada chat tiene su propio estado: varios clientes no se interfieren.
//   * Las acciones de WhatsApp son inyectadas: el gestor no conoce el cliente real y se prueba sin red.
export interface TypingActions {
  typing(jid: string): Promise<void>;
  clear(jid: string): Promise<void>;
}

export interface TypingOptions {
  refreshMs?: number;
  maxMs?: number;
}

interface Active {
  refresh: NodeJS.Timeout;
  expiry: NodeJS.Timeout;
}

export class TypingManager {
  private readonly active = new Map<string, Active>();
  private readonly refreshMs: number;
  private readonly maxMs: number;

  constructor(private readonly actions: TypingActions, options: TypingOptions = {}) {
    this.refreshMs = options.refreshMs ?? 20_000;
    this.maxMs = options.maxMs ?? 60_000;
  }

  isActive(jid: string): boolean {
    return this.active.has(jid);
  }

  async start(jid: string): Promise<boolean> {
    // Ya activo: solo se reinicia la caducidad; no se duplican temporizadores.
    const existing = this.active.get(jid);
    if (existing) {
      clearTimeout(existing.expiry);
      existing.expiry = this.expiryFor(jid);
      return true;
    }
    try {
      await this.actions.typing(jid);
    } catch {
      return false; // sin estado activo: nada que limpiar después
    }
    const refresh = setInterval(() => {
      this.actions.typing(jid).catch(() => undefined);
    }, this.refreshMs);
    this.active.set(jid, { refresh, expiry: this.expiryFor(jid) });
    return true;
  }

  async stop(jid: string): Promise<void> {
    const entry = this.active.get(jid);
    if (!entry) return;
    this.release(jid, entry);
    try {
      await this.actions.clear(jid);
    } catch {
      // Si la limpieza falla, WhatsApp caduca el estado por sí solo; no bloquea el envío.
    }
  }

  async stopAll(): Promise<void> {
    await Promise.all([...this.active.keys()].map((jid) => this.stop(jid)));
  }

  private expiryFor(jid: string): NodeJS.Timeout {
    return setTimeout(() => {
      const entry = this.active.get(jid);
      if (!entry) return;
      this.release(jid, entry);
      this.actions.clear(jid).catch(() => undefined);
    }, this.maxMs);
  }

  private release(jid: string, entry: Active): void {
    clearInterval(entry.refresh);
    clearTimeout(entry.expiry);
    this.active.delete(jid);
  }
}
