import axios, { AxiosError, AxiosInstance, AxiosRequestConfig } from "axios";

const BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

const ACCESS_TOKEN_KEY = "rag-access-token";
const REFRESH_TOKEN_KEY = "rag-refresh-token";

function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function setTokens(access: string, refresh?: string) {
  if (typeof window === "undefined") return;
  localStorage.setItem(ACCESS_TOKEN_KEY, access);
  if (refresh) localStorage.setItem(REFRESH_TOKEN_KEY, refresh);
}

export function clearTokens() {
  if (typeof window === "undefined") return;
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

const client: AxiosInstance = axios.create({ baseURL: BASE_URL });

client.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

let isRefreshing = false;
let refreshQueue: Array<(t: string | null) => void> = [];

async function refreshAccessToken(): Promise<string | null> {
  const refresh = getRefreshToken();
  if (!refresh) return null;
  try {
    const r = await axios.post(`${BASE_URL}/v1/auth/refresh`, { refresh_token: refresh });
    const newAccess = r.data.access_token;
    setTokens(newAccess);
    return newAccess;
  } catch {
    return null;
  }
}

client.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as AxiosRequestConfig & { _retry?: boolean };
    if (error.response?.status === 401 && original && !original._retry) {
      original._retry = true;
      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          refreshQueue.push((token) => {
            if (!token) return reject(error);
            original.headers = original.headers || {};
            (original.headers as Record<string, string>).Authorization = `Bearer ${token}`;
            resolve(client(original));
          });
        });
      }
      isRefreshing = true;
      const newToken = await refreshAccessToken();
      isRefreshing = false;
      refreshQueue.forEach((cb) => cb(newToken));
      refreshQueue = [];
      if (!newToken) {
        clearTokens();
        if (typeof window !== "undefined") window.location.href = "/login";
        return Promise.reject(error);
      }
      original.headers = original.headers || {};
      (original.headers as Record<string, string>).Authorization = `Bearer ${newToken}`;
      return client(original);
    }
    return Promise.reject(error);
  }
);

export default client;
