import { requestConsent } from "./ui/ExternalConsent";
import { coverConfirmation } from "./lib/consent";

/** API 客户端：默认同源相对路径（vite dev/preview 代理到后端，生产同域部署），
 * 会话 Cookie 才能随请求携带；可用 VITE_API_BASE 覆盖。不打印密钥/正文到控制台。 */
const API = (import.meta as any).env?.VITE_API_BASE ?? "";

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
    ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
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
      status: number; code: string; detail: any;
    };
    err.status = res.status;
    err.code = detail.error_code || "ERROR";
    err.detail = detail;
    throw err;
  }
  return data;
}

async function lessonAction(courseId: string, lessonId: string, purpose: "start" | "prewarm" | "recover" = "start", conversationId?: string) {
  const path = purpose === "recover"
    ? `/api/teaching/${encodeURIComponent(conversationId || "")}/recover`
    : `/api/courses/${encodeURIComponent(courseId)}/lessons/${encodeURIComponent(lessonId)}/${purpose}`;
  return req(path, { method: "POST" });
}
async function authorizedCover(kind: string, id: string, regenerate = false) {
  const preflight = await req(`/api/planning/${kind}/${id}/cover/preflight`);
  if (preflight.cover_eligible === false || preflight.status === "suppressed") return preflight;
  const choices = await requestConsent({ title: "确认生成封面", subject: preflight.title,
    content: preflight.config, configs: preflight.configs, cover: true });
  if (!choices) return null;
  if (choices.procedural) return req(`/api/planning/${kind}/${id}/cover`, { method: "POST", body: JSON.stringify({ method: "procedural", regenerate, idempotency_key: `web-cover-${crypto.randomUUID()}` }) });
  return req(`/api/planning/${kind}/${id}/cover/automatic`, { method: "POST", body: JSON.stringify({ ...coverConfirmation(preflight, choices, `web-cover-${crypto.randomUUID()}`), regenerate }) });
}

export const api = {
  streamTeachingContent: async (taskId: string, onEvent: (data: any) => void, signal: AbortSignal) => {
    let lastId = "";
    for (let retry = 0; retry < 4 && !signal.aborted; retry++) {
      const headers: Record<string, string> = { Accept: "text/event-stream" };
      if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
      if (lastId) headers["Last-Event-ID"] = lastId;
      const response = await fetch(`${API}/api/teaching/tasks/${taskId}/content-events`, {
        headers, credentials: "include", signal,
      });
      if (!response.ok || !response.body) throw new Error("讲解内容流暂不可用");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        while (!signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          let split: number;
          while ((split = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, split); buffer = buffer.slice(split + 2);
            const lines = frame.split("\n");
            const id = lines.find(line => line.startsWith("id:"))?.slice(3).trim();
            const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trim()).join("\n");
            if (!data) continue;
            const event = JSON.parse(data);
            if (id) lastId = id;
            onEvent(event);
            if (event.event === "done" || event.event === "error") return;
          }
        }
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    }
  },
  unitAssessment: (course: string, unit: string) => req(`/api/courses/${course}/units/${unit}/assessment`),
  generateAssessment: (course: string, unit: string, key: string) => req(`/api/courses/${course}/units/${unit}/assessment`, { method: "POST", body: JSON.stringify({ idempotency_key: key }) }),
  getAssessment: (id: string) => req(`/api/assessments/${id}`),
  submitAssessment: (id: string, answers: Record<string, string | string[]>, key: string) => req(`/api/assessments/${id}/attempts`, { method: "POST", body: JSON.stringify({ answers, idempotency_key: key }) }),
  assessmentAttempt: (id: string) => req(`/api/assessment-attempts/${id}`),
  retryAssessment: (id: string) => req(`/api/assessment-tasks/${id}/retry`, { method: "POST" }),
  completeLesson: (id: string) => req(`/api/teaching/${id}/complete`, { method: "POST" }),
  planningDialogue: (id: string) => req(`/api/planning/drafts/${id}/dialogue`),
  sendPlanningDialogue: (id: string, body: Record<string, unknown>) =>
    req(`/api/planning/drafts/${id}/dialogue`, { method: "POST", body: JSON.stringify(body) }),
  adoptPlanningChange: (id: string, revision: number) =>
    req(`/api/planning/dialogue/${id}/adopt`, { method: "POST", body: JSON.stringify({ revision }) }),
  discardPlanningChange: (id: string, revision: number) =>
    req(`/api/planning/dialogue/${id}/discard`, { method: "POST", body: JSON.stringify({ revision }) }),
  retryPlanningDialogue: (id: string) => req(`/api/planning/dialogue/${id}/retry`, { method: "POST" }),
  getCover: (kind: string, id: string) => req(`/api/planning/${kind}/${id}/cover`),
  generateCover: (kind: string, id: string, body: Record<string, unknown>) =>
    req(`/api/planning/${kind}/${id}/cover`, { method: "POST", body: JSON.stringify(body) }),
  runCover: (id: string) => req(`/api/planning/covers/${id}/run`, { method: "POST" }),
  cancelTask: (id: string) => req(`/api/tasks/${id}/cancel`, { method: "POST" }),
  coverImage: async (path: string) => {
    const headers: Record<string, string> = {};
    if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
    const response = await fetch(`${API}${path}`, { headers, credentials: "include" });
    if (!response.ok) throw new Error("封面加载失败");
    return URL.createObjectURL(await response.blob());
  },
  register: (email: string, password: string, display_name = "") =>
    req("/api/auth/register", { method: "POST", body: JSON.stringify({ email, password, display_name }) }),
  verifyEmail: (token: string) =>
    req("/api/auth/verify-email", { method: "POST", body: JSON.stringify({ token }) }),
  login: async (email: string, password: string, remember_me = false) => {
    const data = await req("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password, remember_me }),
    });
    return data;
  },
  logout: () => req("/api/auth/logout", { method: "POST" }).finally(() => setSessionToken(null)),
  me: () => req("/api/auth/me"),
  /** 访客引导：未登录时签发/复用访客 Cookie，后续请求以访客身份访问自己的资源。 */
  guest: () => req("/api/auth/guest"),
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
  listTtsVoices: (body: {endpoint:string;api_key:string}) =>
    req("/api/capabilities/tts/voices", {method:"POST",body:JSON.stringify(body)}),
  updateConfig: (configId: string, body: Record<string, unknown>) =>
    req(`/api/capabilities/configs/${configId}`, { method: "PATCH", body: JSON.stringify(body) }),
  deactivateConfig: (configId: string) =>
    req(`/api/capabilities/configs/${configId}`, { method: "DELETE" }),
  testConfig: (configId: string) =>
    req(`/api/capabilities/configs/${configId}/test`, { method: "POST" }),
  listDisclosures: (taskId: string) =>
    req(`/api/disclosures?task_id=${encodeURIComponent(taskId)}`),
  confirmDisclosure: (body: Record<string, unknown>) =>
    req("/api/disclosures", { method: "POST", body: JSON.stringify(body) }),
  revokeDisclosure: (grantId: string) =>
    req(`/api/disclosures/${grantId}/revoke`, { method: "POST" }),
  createPairing: (deviceName: string) =>
    req("/api/connectors/pairing", { method: "POST", body: JSON.stringify({ device_name: deviceName }) }),
  listConnectors: () => req("/api/connectors"),
  revokeConnector: (bindingId: string) =>
    req(`/api/connectors/${bindingId}/revoke`, { method: "POST" }),
  taskSse: (taskId: string, lastEventId?: string, signal?: AbortSignal) => {
    const headers: Record<string, string> = {};
    if (sessionToken) headers["Authorization"] = `Bearer ${sessionToken}`;
    if (lastEventId) headers["Last-Event-ID"] = lastEventId;
    return fetch(`${API}/api/tasks/${taskId}/sse`, { headers, credentials: "include", signal });
  },

  listMaterials: () => req("/api/materials"),
  uploadMaterials: (files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return req("/api/materials", { method: "POST", body: fd });
  },
  getMaterial: (materialId: string) => req(`/api/materials/${materialId}`),
  getMaterialPage: (materialId: string, pageNo: number) =>
    req(`/api/materials/${materialId}/pages/${pageNo}`),
  retryMaterialPage: (materialId: string, pageNo: number) =>
    req(`/api/materials/${materialId}/pages/${pageNo}/retry`, { method: "POST", body: JSON.stringify({ reason: "user_retry" }) }),
  deleteMaterial: (materialId: string) =>
    req(`/api/materials/${materialId}`, { method: "DELETE" }),
  searchMaterials: (body: Record<string, unknown>) =>
    req("/api/materials/search", { method: "POST", body: JSON.stringify(body) }),
  // 切片 4 补充：流式即时回答（返回原始 Response，调用方解析 SSE）
  streamChat: (conversationId: string, body: Record<string, unknown>) => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (sessionToken) headers["Authorization"] = `Bearer ${sessionToken}`;
    return fetch(`${API}/api/chat/${conversationId}/messages/stream`,
      { method: "POST", headers, credentials: "include", body: JSON.stringify(body) });
  },
  // 切片 4：讲次教学
  startLessonV2: lessonAction,
  prewarmLesson: (courseId: string, lessonId: string) => lessonAction(courseId, lessonId, "prewarm"),
  recoverLesson: (courseId: string, lessonId: string, conversationId: string) => lessonAction(courseId, lessonId, "recover", conversationId),
  authorizeCover: authorizedCover,
  lessonAuthorizationPreflight: (courseId: string, lessonId: string, purpose: "start" | "recover" = "start", conversationId?: string) =>
    req(`/api/courses/${courseId}/lessons/${lessonId}/authorization-preflight?purpose=${purpose}${conversationId ? `&conversation_id=${encodeURIComponent(conversationId)}` : ""}`),
  confirmLessonAuthorization: (courseId: string, lessonId: string, body: Record<string, unknown>) =>
    req(`/api/courses/${courseId}/lessons/${lessonId}/authorization-confirm`, { method: "POST", body: JSON.stringify(body) }),
  coverPreflight: (kind: string, id: string) => req(`/api/planning/${kind}/${id}/cover/preflight`),
  generateAutomaticCover: (kind: string, id: string, body: Record<string, unknown>) =>
    req(`/api/planning/${kind}/${id}/cover/automatic`, { method: "POST", body: JSON.stringify(body) }),
  getWorkspace: (conversationId: string) => req(`/api/teaching/${conversationId}`),
  prewarmLibrary:()=>req('/api/teaching/prewarm-library',{method:'POST'}),
  firstPreparation:(courseId:string)=>req(`/api/courses/${courseId}/first-preparation`),
  upgradeLecture: (conversationId: string) => req(`/api/teaching/${conversationId}/upgrade`, {method:"POST"}),
  saveCheckpoint: (conversationId: string, body: Record<string, unknown>) =>
    req(`/api/teaching/${conversationId}/checkpoint`, { method: "POST", body: JSON.stringify(body) }),
  sendFollowup: (conversationId: string, body: Record<string, unknown>) =>
    req(`/api/teaching/${conversationId}/followup`, { method: "POST", body: JSON.stringify(body) }),
  resumeLesson: (conversationId: string) =>
    req(`/api/teaching/${conversationId}/resume`, { method: "POST" }),
  runTeachingTask: (taskId: string) =>
    req(`/api/teaching/tasks/${taskId}/run`, { method: "POST" }),
  audioUrl: (audioId: string) => `${API}/api/teaching/audio/${audioId}/content`,
  ensureLessonAudio:(conversationId:string,stepNo:number)=>req(`/api/teaching/${conversationId}/audio`,
    {method:"POST",body:JSON.stringify({step_no:stepNo})}),
  figureUrl: (materialId: string, figureId: string) =>
    `${API}/api/materials/${materialId}/figures/${figureId}/content`,
  // 切片 3：规划 / 草稿 / 场景 / 总览 / 导航 / 即时对话
  createRoute: (title: string, scenario = "self_study", input = "") =>
    req("/api/planning/routes", { method: "POST", body: JSON.stringify({ title, scenario, input }) }),
  createCourse: (body: Record<string, unknown>) =>
    req("/api/planning/courses", { method: "POST", body: JSON.stringify(body) }),
  listCourses: () => req("/api/planning/courses"),
  listLibrary: () => req("/api/planning/library"),
  inputMatches: (input: string) => req("/api/planning/input-matches", { method: "POST", body: JSON.stringify({ input }) }),
  draftDuplicates: (id: string, revision: number) => req(`/api/planning/drafts/${id}/duplicates`, {
    method: "POST", body: JSON.stringify({ revision }) }),
  editPlanningDraft: (kind: string, id: string) => req(`/api/planning/${kind}/${id}/edit-draft`, { method: "POST" }),
  deleteCourse: (id: string) => req(`/api/planning/courses/${id}`, { method: "DELETE" }),
  deleteRoute: (id: string) => req(`/api/planning/routes/${id}`, { method: "DELETE" }),
  detachCourse: (routeId: string, courseId: string) => req(`/api/routes/${routeId}/courses/${courseId}/detach`, { method: "POST" }),
  getNavigation: () => req("/api/navigation"),
  adminPending: () => req("/api/auth/admin/pending"),
  createPlanningTask: (body: Record<string, unknown>) =>
    req("/api/planning/tasks", { method: "POST", body: JSON.stringify(body) }),
  runPlanningTask: (taskId: string) =>
    req(`/api/planning/tasks/${taskId}/run`, { method: "POST" }),
  getPlanningTask: (taskId: string) => req(`/api/planning/tasks/${taskId}`),
  currentPlanning: () => req("/api/planning/current"),
  resumePlanning: (taskId: string, automatic = false) => req(`/api/planning/tasks/${taskId}/resume`, {
    method: "POST", body: JSON.stringify({ automatic }),
  }),
  abandonPlanning: (taskId: string) => req(`/api/planning/tasks/${taskId}/abandon`, { method: "POST" }),
  savePlanningAnswers: (taskId: string, revision: number, answers: any[]) => req(`/api/planning/tasks/${taskId}/answer-draft`, {
    method: "PUT", keepalive: true, body: JSON.stringify({ revision, answers }),
  }),
  decidePlanningTask: (taskId: string, body: Record<string, unknown>) =>
    req(`/api/planning/tasks/${taskId}/decision`, { method: "POST", body: JSON.stringify(body) }),
  getDraft: (draftId: string) => req(`/api/planning/drafts/${draftId}`),
  saveDraft: (draftId: string, body: Record<string, unknown>) =>
    req(`/api/planning/drafts/${draftId}`, { method: "PUT", body: JSON.stringify(body) }),
  confirmDraft: (draftId: string, body: Record<string, unknown>) =>
    req(`/api/planning/drafts/${draftId}/confirm`, { method: "POST", body: JSON.stringify(body) }),
  switchDraftScope: (draftId: string, planningScope: string) =>
    req(`/api/planning/drafts/${draftId}/scope`, { method: "POST", body: JSON.stringify({ planning_scope: planningScope }) }),
  routeOverview: (routeId: string, versionId?: string) => req(`/api/routes/${routeId}/overview${versionId ? `?version_id=${encodeURIComponent(versionId)}` : ""}`),
  courseOverview: (courseId: string, context = "") => req(`/api/courses/${courseId}/overview${context ? `?${context}` : ""}`),
  routeScene: (routeId: string, versionId?: string) => req(`/api/routes/${routeId}/scene${versionId ? `?version_id=${encodeURIComponent(versionId)}` : ""}`),
  courseScene: (courseId: string) => req(`/api/courses/${courseId}/scene`),
  refineDraft: (draftId: string, revision: number, courseIndex?: number) =>
    req(`/api/planning/drafts/${draftId}/refine`, { method: "POST", body: JSON.stringify({ revision, course_index: courseIndex }) }),
  draftScene: (draftId: string) => req(`/api/planning/drafts/${draftId}/scene`),
  getViewState: (targetKind: string, targetId: string) =>
    req(`/api/planning/viewstates/${targetKind}/${targetId}`),
  putViewState: (targetKind: string, targetId: string, canvas: Record<string, unknown>) =>
    req(`/api/planning/viewstates/${targetKind}/${targetId}`, { method: "PUT", body: JSON.stringify({ canvas }) }),
  putNavigation: (body: Record<string, unknown>) =>
    req("/api/navigation", { method: "PUT", body: JSON.stringify(body) }),
  startLesson: (courseId: string, lessonId: string) => lessonAction(courseId, lessonId),
  createChat: (body: Record<string, unknown>) =>
    req("/api/chat", { method: "POST", body: JSON.stringify(body) }),
  getChat: (conversationId: string) => req(`/api/chat/${conversationId}`),
  sendChat: (conversationId: string, body: Record<string, unknown>) =>
    req(`/api/chat/${conversationId}/messages`, { method: "POST", body: JSON.stringify(body) }),
  resumeChat: (conversationId: string, decision: string) =>
    req(`/api/chat/${conversationId}/resume`, { method: "POST", body: JSON.stringify({ decision }) }),
};
