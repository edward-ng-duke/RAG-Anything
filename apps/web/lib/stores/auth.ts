import { create } from "zustand";
import { persist } from "zustand/middleware";
import { setTokens as setApiTokens, clearTokens as clearApiTokens } from "@/lib/api/client";
import type { UserInfo, TenantBrief } from "@/lib/api/types";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: UserInfo | null;
  tenants: TenantBrief[];
  currentTenantId: string | null;

  setSession: (params: {
    accessToken: string;
    refreshToken: string;
    user: UserInfo;
    tenants: TenantBrief[];
  }) => void;
  setAccessToken: (token: string) => void;
  selectTenant: (tenant_id: string) => void;
  clear: () => void;
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      tenants: [],
      currentTenantId: null,

      setSession: ({ accessToken, refreshToken, user, tenants }) => {
        setApiTokens(accessToken, refreshToken);
        set({
          accessToken,
          refreshToken,
          user,
          tenants,
          currentTenantId: tenants[0]?.tenant_id ?? null,
        });
      },

      setAccessToken: (token) => {
        setApiTokens(token);
        set({ accessToken: token });
      },

      selectTenant: (tenant_id) => set({ currentTenantId: tenant_id }),

      clear: () => {
        clearApiTokens();
        set({
          accessToken: null,
          refreshToken: null,
          user: null,
          tenants: [],
          currentTenantId: null,
        });
      },
    }),
    {
      name: "rag-auth",
      partialize: (state) => ({
        accessToken: state.accessToken,
        refreshToken: state.refreshToken,
        user: state.user,
        tenants: state.tenants,
        currentTenantId: state.currentTenantId,
      }),
    }
  )
);
