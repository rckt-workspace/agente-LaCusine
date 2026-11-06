// Cliente de la API administrativa. Errores traducidos a mensajes en español; nunca se muestra texto del servidor.

export class ApiError extends Error {
  constructor(public status: number, public code: string) {
    super(code);
  }
}

const MESSAGES: Record<string, string> = {
  invalid_token: "Tu sesión expiró. Vuelve a iniciar sesión.",
  forbidden: "Tu cuenta no tiene permiso para esta acción.",
  not_found: "No encontramos el registro solicitado.",
  transition_not_allowed: "Esta acción no es válida en el estado actual del caso.",
  state_changed_concurrently: "Otro operador cambió este caso. Recarga y vuelve a intentarlo.",
  storage_unavailable: "La base de datos no responde en este momento. Intenta de nuevo.",
  network_error: "No hay conexión con el servidor.",
};

export function messageFor(error: unknown): string {
  if (error instanceof ApiError) return MESSAGES[error.code] ?? "No se pudo completar la operación.";
  return MESSAGES.network_error;
}

async function request<T>(method: string, path: string, token: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/admin${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (!response.ok) {
    let code = "request_failed";
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") code = data.detail;
    } catch {
      // Respuesta sin cuerpo JSON: se mantiene el código genérico.
    }
    throw new ApiError(response.status, code);
  }
  return (await response.json()) as T;
}

export const api = {
  get: <T,>(path: string, token: string) => request<T>("GET", path, token),
  post: <T,>(path: string, token: string, body: unknown) => request<T>("POST", path, token, body),
};

export type Page<T> = { items: T[]; total: number; limit: number; offset: number };
