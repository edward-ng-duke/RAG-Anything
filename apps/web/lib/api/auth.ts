import client from "./client";
import type { AuthTokens, MeResponse } from "./types";

export async function signup(payload: { email: string; password: string; display_name?: string }) {
  const r = await client.post<AuthTokens>("/v1/auth/signup", payload);
  return r.data;
}

export async function login(payload: { email: string; password: string }) {
  const r = await client.post<AuthTokens>("/v1/auth/login", payload);
  return r.data;
}

export async function me() {
  const r = await client.get<MeResponse>("/v1/auth/me");
  return r.data;
}

export async function logout() {
  await client.post("/v1/auth/logout").catch(() => undefined);
}

export async function selectTenant(tenant_id: string) {
  const r = await client.post<{ access_token: string; tenant_id: string }>("/v1/auth/select_tenant", { tenant_id });
  return r.data;
}
