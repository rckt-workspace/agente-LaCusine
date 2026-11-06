import { useState, type FormEvent } from "react";
import { useAuth } from "../auth";
import { authConfigured } from "../supabase";
import { buttonClass, inputClass } from "../ui";

export function Login() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const result = await signIn(email.trim(), password);
    setBusy(false);
    if (result === "auth_not_configured") setError("El inicio de sesión no está configurado en este despliegue.");
    else if (result) setError("Correo o contraseña incorrectos.");
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 px-4">
      <div className="w-full max-w-sm rounded-2xl bg-white p-8 shadow-md">
        <p className="text-xs font-semibold uppercase tracking-widest text-indigo-600">La Cuisine Appliances Colombia</p>
        <h1 className="mt-2 text-xl font-semibold text-slate-900">Panel de atención</h1>
        {!authConfigured && (
          <p className="mt-4 rounded-md bg-amber-50 p-3 text-sm text-amber-800">
            Falta la configuración de Supabase Auth en este build (VITE_SUPABASE_URL y VITE_SUPABASE_PUBLISHABLE_KEY).
          </p>
        )}
        <form onSubmit={submit} className="mt-6 space-y-4">
          <label className="block text-sm font-medium text-slate-700">
            Correo
            <input className={`${inputClass} mt-1`} type="email" autoComplete="username" required value={email}
              onChange={(e) => setEmail(e.target.value)} disabled={!authConfigured} />
          </label>
          <label className="block text-sm font-medium text-slate-700">
            Contraseña
            <input className={`${inputClass} mt-1`} type="password" autoComplete="current-password" required value={password}
              onChange={(e) => setPassword(e.target.value)} disabled={!authConfigured} />
          </label>
          {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
          <button type="submit" className={`${buttonClass} w-full`} disabled={busy || !authConfigured}>
            {busy ? "Ingresando…" : "Ingresar"}
          </button>
        </form>
      </div>
    </div>
  );
}
