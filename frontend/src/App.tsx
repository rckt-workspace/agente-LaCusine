import { useState } from "react";
import { api } from "./api";
import { AuthProvider, useAuth } from "./auth";
import { Layout, type Route } from "./Layout";
import { Analytics } from "./pages/Analytics";
import { Commercial } from "./pages/Commercial";
import { Conversations } from "./pages/Conversations";
import { Dashboard } from "./pages/Dashboard";
import { Leads } from "./pages/Leads";
import { Login } from "./pages/Login";
import { Products } from "./pages/Products";
import { Settings } from "./pages/Settings";
import { ErrorBox, Loading, useFetch } from "./ui";

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}

function Shell() {
  const { token, loading } = useAuth();
  if (loading) return <Loading />;
  if (!token) return <Login />;
  return <Authenticated token={token} />;
}

function Authenticated({ token }: { token: string }) {
  const [route, setRoute] = useState<Route>("dashboard");
  const me = useFetch<{ role: string }>(() => api.get<{ role: string }>("/me", token), [token]);

  if (me.loading) return <Loading />;
  if (me.error || !me.data) {
    return (
      <div className="mx-auto max-w-md p-8">
        <ErrorBox message={me.error ?? "No se pudo cargar tu cuenta."} onRetry={me.reload} />
      </div>
    );
  }
  const role = me.data.role;

  return (
    <Layout route={route} role={role} onNavigate={setRoute}>
      {route === "dashboard" && <Dashboard onNavigate={setRoute} />}
      {route === "conversations" && <Conversations role={role} />}
      {route === "leads" && <Leads />}
      {route === "analytics" && <Analytics />}
      {route === "commercial" && <Commercial role={role} />}
      {route === "products" && <Products />}
      {route === "settings" && <Settings role={role} />}
    </Layout>
  );
}
