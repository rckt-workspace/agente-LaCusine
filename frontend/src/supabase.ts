import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Solo la clave publicable entra al navegador. Nunca la clave secreta ni credenciales de base de datos.
const url = import.meta.env.VITE_SUPABASE_URL;
const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY;

export const authConfigured = Boolean(url && key);

export const supabase: SupabaseClient | null = authConfigured
  ? createClient(url as string, key as string, { auth: { persistSession: true, autoRefreshToken: true } })
  : null;
