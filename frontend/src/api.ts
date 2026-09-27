/** API 客户端：只访问本机开发 API，不打印密钥/正文到控制台。 */
const API = (import.meta as any).env?.VITE_API_BASE || "http://127.0.0.1:8000";

// 浏览器会话由服务端 HttpOnly cookie 承载；令牌只在内存中保留，供开发测试的 Bearer 兼容路径使用。
let sessionToken: string | null = null;

export function setSessionToken(token: string | null) {
  sessionToken = token;
}

export function getSessionToken() {
  return sessionToken;
}

async function req(path: string, init: RequestInit = {}) {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (sessionToken) headers["Authorization"] = `Bearer ${sessionToken}`;
  const res = await fetch(`${API}${path}`, { ...init, headers, credentials: "include" });
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    const detail = data?.detail || data || {};
    const err = new Error(detail.message || detail.error_code || res.statusText) as Error & {
      status: number; code: string;
    };
    err.status = res.status;
    err.code = detail.error_code || "ERROR";
    throw err;
  }
  return data;
}

export const api = {
  register: (email: string, password: string, display_name = "") =>
    req("/api/auth/register", { method: "POST", body: JSON.stringify({ email, password, display_name }) }),
  verifyEmail: (token: string) =>
    req("/api/auth/verify-email", { method: "POST", body: JSON.stringify({ token }) }),
  login: async (email: string, password: string) => {
    const data = await req("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    return data;
  },
  logout: () => req("/api/auth/logout", { method: "POST" }).finally(() => setSessionToken(null)),
  me: () => req("/api/auth/me"),
  approve: (user_id: string) => req("/api/auth/admin/approve", { method: "POST", body: JSON.stringify({ user_id }) }),
  reject: (user_id: string, reason = "") =>
    req("/api/auth/admin/reject", { method: "POST", body: JSON.stringify({ user_id, reason }) }),
  disable: (user_id: string) =>
    req("/api/auth/admin/disable", { method: "POST", body: JSON.stringify({ user_id }) }),
  resetRequest: (email: string) =>
    req("/api/auth/password-reset/request", { method: "POST", body: JSON.stringify({ email }) }),
  resetConfirm: (token: string, new_password: string) =>
    req("/api/auth/password-reset/confirm", { method: "POST", body: JSON.stringify({ token, new_password }) }),
  createTask: (body: Record<string, unknown>) =>
    req("/api/tasks", { method: "POST", body: JSON.stringify(body) }),
  taskStatus: (taskId: string) => req(`/api/tasks/${taskId}/status`),
  // 切片 1：能力配置 / 外发确认 / 连接器
  listConfigs: (kind?: string) =>
    req(`/api/capabilities/configs${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`),
  createConfig: (body: Record<string, unknown>) =>
    req("/api/capabilities/configs", { method: "POST", body: JSON.stringify(body) }),
  updateConfig: (configId: string, body: Record<string, unknown>) =>
    req(`/api/capabilities/configs/${configId}`, { method: "PATCH", body: JSON.stringify(body) }),
  deactivateConfig: (configId: string) =>
    req(`/api/capabilities/configs/${configId}`, { method: "DELETE" }),
  testConfig: (configId: string) =>
    req(`/api/capabilities/configs/${configId}/test`, { method: "POST" }),
  listDisclosures: (taskId: string) =>
    req(`/api/disclosures?task_id=${encodeURIComponent(taskId)}`),
  revokeDisclosure: (grantId: string) =>
    req(`/api/disclosures/${grantId}/revoke`, { method: "POST" }),
  createPairing: (deviceName: string) =>
    req("/api/connectors/pairing", { method: "POST", body: JSON.stringify({ device_name: deviceName }) }),
  listConnectors: () => req("/api/connectors"),
  revokeConnector: (bindingId: string) =>
    req(`/api/connectors/${bindingId}/revoke`, { method: "POST" }),
  taskSse: (taskId: string, lastEventId?: string) => {
    const headers: Record<string, string> = {};
    if (sessionToken) headers["Authorization"] = `Bearer ${sessionToken}`;
    if (lastEventId) headers["Last-Event-ID"] = lastEventId;
    return fetch(`${API}/api/tasks/${taskId}/sse`, { headers, credentials: "include" });
  },
};
