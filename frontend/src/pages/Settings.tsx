import { useAuth } from "../auth";
import { Card } from "../ui";

// Estado honesto: el módulo de conexión oficial de Meta aún no está implementado. No hay conexión que mostrar.
const STEPS = [
  "Crear o usar una app de Meta con el producto WhatsApp y la cuenta empresarial verificada.",
  "Configurar el webhook (URL HTTPS pública, token de verificación y firma con el secreto de la app).",
  "Probar primero con el número de prueba de Meta y destinatarios permitidos.",
  "Conectar el número comercial solo con autorización expresa del responsable.",
];

export function Settings({ role }: { role: string }) {
  const { signOut } = useAuth();
  return (
    <div className="max-w-3xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900">Configuración</h1>
        <p className="text-sm text-slate-500">Estado real de la integración. Lo que no está implementado se indica como tal.</p>
      </header>

      <Card title="Conexión de WhatsApp">
        <div className="space-y-3 text-sm">
          <p className="flex items-center gap-2">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-slate-400" aria-hidden="true" />
            <span className="font-medium text-slate-800">No conectado</span>
          </p>
          <p className="text-slate-600">
            El botón «Conectar WhatsApp» (flujo oficial de Meta WhatsApp Cloud API) todavía no está implementado.
            Mientras tanto no se envía ningún mensaje real desde este panel.
          </p>
          <ol className="list-decimal space-y-1 pl-5 text-slate-600">
            {STEPS.map((s) => <li key={s}>{s}</li>)}
          </ol>
          <button disabled className="cursor-not-allowed rounded-md border border-dashed border-slate-300 px-3 py-2 text-sm text-slate-400">
            Conectar WhatsApp (pendiente de implementación)
          </button>
        </div>
      </Card>

      <Card title="Cuenta">
        <dl className="grid grid-cols-2 gap-3 text-sm">
          <div><dt className="text-slate-500">Rol</dt><dd>{role === "admin" ? "Administrador" : "Operador"}</dd></div>
          <div><dt className="text-slate-500">Acciones de reanudar IA</dt><dd>{role === "admin" ? "Permitidas" : "Solo administradores"}</dd></div>
        </dl>
        <button onClick={() => void signOut()} className="mt-4 text-sm text-indigo-700 underline">Cerrar sesión</button>
      </Card>
    </div>
  );
}
