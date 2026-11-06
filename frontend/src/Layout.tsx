import type { ReactNode } from "react";
import { useAuth } from "./auth";

export type Route = "dashboard" | "conversations" | "leads" | "analytics" | "commercial" | "products" | "settings";

const NAV: { id: Route; label: string }[] = [
  { id: "dashboard", label: "Panel" },
  { id: "conversations", label: "Conversaciones" },
  { id: "leads", label: "Leads" },
  { id: "analytics", label: "Analítica" },
  { id: "commercial", label: "Inteligencia comercial" },
  { id: "products", label: "Catálogo" },
  { id: "settings", label: "Configuración" },
];

export function Layout({ route, role, onNavigate, children }: {
  route: Route; role: string; onNavigate: (r: Route) => void; children: ReactNode;
}) {
  const { signOut } = useAuth();
  return (
    <div className="min-h-screen md:flex">
      <aside className="border-b border-slate-200 bg-slate-900 text-slate-100 md:flex md:w-60 md:flex-col md:border-b-0">
        <div className="px-4 py-4">
          <p className="text-xs uppercase tracking-widest text-indigo-300">La Cuisine</p>
          <p className="font-semibold">Appliances Colombia</p>
        </div>
        <nav aria-label="Principal" className="flex gap-1 overflow-x-auto px-2 pb-3 md:flex-col md:overflow-visible">
          {NAV.map((item) => (
            <button key={item.id} onClick={() => onNavigate(item.id)} aria-current={route === item.id ? "page" : undefined}
              className={`whitespace-nowrap rounded-md px-3 py-2 text-left text-sm ${
                route === item.id ? "bg-indigo-600 text-white" : "text-slate-300 hover:bg-slate-800"}`}>
              {item.label}
            </button>
          ))}
        </nav>
        <div className="flex items-center justify-between gap-2 border-t border-slate-800 px-4 py-3 text-xs text-slate-400 md:mt-auto md:flex-col md:items-start">
          <span>Rol: {role === "admin" ? "Administrador" : "Operador"}</span>
          <button onClick={() => void signOut()} className="text-slate-300 underline hover:text-white">Salir</button>
        </div>
      </aside>
      <main className="flex-1 px-4 py-6 md:px-8">{children}</main>
    </div>
  );
}
