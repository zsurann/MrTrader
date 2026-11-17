// Public, read-only credentials (anon role: SELECT only, enforced by RLS).
// Set these at build time. Never put a service or secret role key here.
export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
export const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";
