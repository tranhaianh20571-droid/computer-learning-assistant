import { requestConsent } from "./ui/ExternalConsent";
import { coverConfirmation } from "./lib/consent";

/** API 客户端：默认同源相对路径（vite dev/preview 代理到后端，生产同域部署），
 * 会话 Cookie 才能随请求携带；可用 VITE_API_BASE 覆盖。不打印密钥/正文到控制台。 */
const API = (import.meta as any).env?.VITE_API_BASE ?? "";

export const reviewApi = {
  overview:(timezone:string)=>req(`/api/review/overview?timezone=${encodeURIComponent(timezone)}`),
  points:(course:string,timezone:string)=>req(`/api/review/courses/${encodeURIComponent(course)}/points?timezone=${encodeURIComponent(timezone)}`),
  create:(course_id:string,idempotency_key:string,timezone:string)=>req("/api/review/sessions",
    {method:"POST",body:JSON.stringify({course_id,idempotency_key,timezone})}),
  get:(id:string)=>req(`/api/review/sessions/${encodeURIComponent(id)}`),
  answer:(id:string,body:unknown)=>req(`/api/review/sessions/${encodeURIComponent(id)}/answers`,
    {method:"POST",body:JSON.stringify(body)}),
  help:(id:string,revision:number)=>req(`/api/review/sessions/${encodeURIComponent(id)}/help`,
    {method:"POST",body:JSON.stringify({revision})}),
  next:(id:string,revision:number,skip=false)=>req(`/api/review/sessions/${encodeURIComponent(id)}/next`,
    {method:"POST",body:JSON.stringify({revision,skip})}),
  retry:(id:string)=>req(`/api/review/sessions/${encodeURIComponent(id)}/retry`,{method:"POST"}),
  draft:(id:string,body:unknown)=>req(`/api/review/sessions/${encodeURIComponent(id)}/draft`,
    {method:"PUT",body:JSON.stringify(body)}),
  cancel:(id:string)=>req(`/api/review/sessions/${encodeURIComponent(id)}/cancel`,{method:"POST"}),
};

export const resourceApi = {
  status:(signal?:AbortSignal)=>req("/api/local-resources",{signal}),
  savePolicy:(policy:unknown,version:number)=>req("/api/local-resources/policy",{method:"PUT",body:JSON.stringify({policy,version})}),
  pin:(id:string,pinned:boolean)=>req(`/api/local-resources/objects/${encodeURIComponent(id)}/pin`,{method:"PUT",body:JSON.stringify({pinned})}),
  preview:()=>req("/api/local-resources/preview",{method:"POST"}),
  cleanup:(resource_ids:string[])=>req("/api/local-resources/cleanup",{method:"POST",body:JSON.stringify({resource_ids})}),
  scan:()=>req("/api/local-resources/scan",{method:"POST"}),
  backup:()=>req("/api/local-resources/backup",{method:"POST"}),
  migrate:(destination:string)=>req("/api/local-resources/migration",{method:"POST",body:JSON.stringify({destination})}),
  retry:(id:string)=>req(`/api/local-resources/jobs/${encodeURIComponent(id)}/retry`,{method:"POST"}),
};

export const recordingApi={
  lesson:(course:string,lesson:string)=>req(`/api/courses/${encodeURIComponent(course)}/lessons/${encodeURIComponent(lesson)}/recordings`),
  list:(id:string)=>req(`/api/teaching/${id}/recordings`),
  start:(id:string,mime_type:string)=>req(`/api/teaching/${id}/recordings`,{method:"POST",body:JSON.stringify({mime_type})}),
  chunk:(id:string,recording:string,sequence:number,body:Blob)=>
    req(`/api/teaching/${id}/recordings/${recording}/chunks/${sequence}`,{method:"PUT",body,headers:{"Content-Type":"video/webm"}},30_000),
  finish:(id:string,recording:string,body:unknown)=>
    req(`/api/teaching/${id}/recordings/${recording}/finish`,{method:"POST",body:JSON.stringify(body)},120_000),
  remove:(id:string,recording:string)=>req(`/api/teaching/${id}/recordings/${recording}`,{method:"DELETE"}),
  video:(id:string,recording:string)=>`${API}/api/teaching/${id}/recordings/${recording}/video`,
};

export const classroomApi = {
  run:(id:string,run:string,body:unknown,signal:AbortSignal)=>
    req(`/api/teaching/${encodeURIComponent(id)}/interactive/runs/${run}`,
      {method:"POST",body:JSON.stringify(body),signal},60_000),
  stop:(id:string,run:string)=>req(`/api/teaching/${encodeURIComponent(id)}/interactive/runs/${run}`,{method:"DELETE"},15_000),
  get:(id:string)=>req(`/api/teaching/${encodeURIComponent(id)}/interactive`),
  save:(id:string,body:unknown)=>req(`/api/teaching/${encodeURIComponent(id)}/interactive`,
    {method:"PUT",body:JSON.stringify(body)}),
  feedback:(id:string,body:unknown)=>req(`/api/teaching/${encodeURIComponent(id)}/interactive/feedback`,
    {method:"POST",body:JSON.stringify(body)}),
  original:(id:string,material:string,sha:string)=>
    `${API}/api/teaching/${encodeURIComponent(id)}/sources/${encodeURIComponent(material)}/${encodeURIComponent(sha)}/original`,
};

export const sourceApi = {
  directory: (url: string, signal?: AbortSignal) => req("/api/source-directory",
    {method:"POST", body:JSON.stringify({url}), signal}, 30_000),
  settings: (provider: string) => req(`/api/source-settings/${provider}`),
  saveSettings: (provider: string, body: unknown) => req(`/api/source-settings/${provider}`, {method: "PUT", body: JSON.stringify(body)}),
  testSettings: (provider: string) => req(`/api/source-settings/${provider}/test`, {method: "POST"}, 125_000),
  create: (body: unknown) => req("/api/source-imports", {method: "POST", body: JSON.stringify(body)}),
  upload: (id: string, item: string, file: File, signal?: AbortSignal) => {
    const body = new FormData(); body.append("file", file);
    return req(`/api/source-imports/${id}/items/${item}/file`, {method: "POST", body, signal}, 300_000);
  },
  prepare: (id: string) => req(`/api/source-imports/${id}/prepare`, {method: "POST"}),
  commit: (id: string, manifest_hash: string) => req(`/api/source-imports/${id}/commit`,
    {method: "POST", body: JSON.stringify({manifest_hash, confirmed: true})}),
  get: (id: string, signal?: AbortSignal) => req(`/api/source-imports/${id}`, {signal}, 15_000),
  cancel: (id: string) => req(`/api/source-imports/${id}`, {method: "DELETE"}),
  retry: (id: string, manifest_hash:string) => req(`/api/source-imports/${id}/retry`,
    {method:"POST",body:JSON.stringify({manifest_hash,confirmed:true})}),
  material: (id: string) => req(`/api/source-materials/${id}`),
};

export const characterApi = {
  preference:(conversation?:string)=>req("/api/mentor-preference"+(conversation?`?conversation_id=${encodeURIComponent(conversation)}`:""),{},10_000),
  use:(body:Record<string,unknown>)=>req("/api/mentor-preference",{method:"PUT",body:JSON.stringify(body)}),
  catalog:(config:string,signal?:AbortSignal)=>req(`/api/character-voices?config_id=${encodeURIComponent(config)}`,{signal},22_000),
  expression:(profile:unknown,config:string,signal?:AbortSignal)=>req("/api/character-expression",
    {method:"POST",body:JSON.stringify({profile,config_id:config}),signal},22_000),
  chooseVoice:(id:string,body:unknown)=>req(`/api/cards/${encodeURIComponent(id)}/voice-choice`,
    {method:"PUT",body:JSON.stringify(body)},22_000),
  previewVoice:async(profile:unknown,config:string,signal?:AbortSignal)=>{
    const response=await fetch(`${API}/api/character-voice-preview`,{method:"POST",credentials:"include",
      signal,headers:{"Content-Type":"application/json",...(sessionToken?{Authorization:`Bearer ${sessionToken}`}:{})},
      body:JSON.stringify({profile,config_id:config})});
    if(!response.ok){const detail=await response.json().catch(()=>({}));throw new Error(detail.message||detail.error_code||"试听生成失败，请重试。");}
    return response.blob();
  },
  copy:(id:string)=>req(`/api/cards/${encodeURIComponent(id)}/copy`,{method:"POST"}),
  exportUrl:(id:string)=>`${API}/api/cards/${encodeURIComponent(id)}/export`,
  importCard:(file:File)=>req("/api/cards/import",{method:"POST",headers:{"Content-Type":"application/zip"},body:file},120_000),
  segmentAssets: (conversationId: string, stepNo: number) => req(`/api/teaching/${conversationId}/character-assets/${stepNo}`),
  list: (market = false, signal?: AbortSignal) => req(market ? "/api/card-market" : "/api/cards", { signal }, 10_000),
  get: (id: string, signal?: AbortSignal) => req(`/api/cards/${encodeURIComponent(id)}`, { signal }, 10_000),
  create: (profile: any) => req("/api/cards", { method: "POST", body: JSON.stringify(profile) }),
  edit: (id: string, revision: number, profile: any) => req(`/api/cards/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ expected_revision: revision, profile }) }),
  publish: (id: string, revision: number) => req(`/api/cards/${encodeURIComponent(id)}/publish?revision=${revision}`, { method: "POST" }),
  collect: (id: string, enabled: boolean) => req(`/api/cards/${encodeURIComponent(id)}/collect`, { method: enabled ? "POST" : "DELETE" }),
  remove: (id: string) => req(`/api/cards/${encodeURIComponent(id)}`, { method: "DELETE" }),
  upload: (id: string, revision: number, file: File, scene: string) => req(`/api/cards/${encodeURIComponent(id)}/assets?revision=${revision}&kind=${file.type.startsWith("video/") ? "video" : "image"}&scene=${scene}`, { method: "POST", headers: { "Content-Type": file.type }, body: file }),
  confirm: (id: string, assetId: string) => req(`/api/cards/${encodeURIComponent(id)}/assets/${encodeURIComponent(assetId)}/confirm`, { method: "POST" }),
  jobs: (id: string, signal?: AbortSignal) => req(`/api/cards/${encodeURIComponent(id)}/media-jobs`, { signal }, 10_000),
  generate: (id: string, request: any) => req(`/api/cards/${encodeURIComponent(id)}/media-jobs`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(request) }),
  cancel: (id: string, taskId: string) => req(`/api/cards/${encodeURIComponent(id)}/media-jobs/${encodeURIComponent(taskId)}`, { method: "DELETE" }),
  voices: (id: string, config: string) => req(`/api/cards/${encodeURIComponent(id)}/voices?config_id=${encodeURIComponent(config)}`),
  bind: (conversation: string, card: string, revision: number, step: number) => req(`/api/teaching/${encodeURIComponent(conversation)}/card-binding?card_id=${encodeURIComponent(card)}&revision=${revision}&effective_step_no=${step}`, { method: "POST" }),
  marketBinding: () => req("/api/market/binding", {}, 15_000),
  marketProfile: () => req("/api/market/profile", {}, 15_000),
  marketWebLogin: () => req("/api/market/web-login", { method: "POST" }, 20_000),
  marketBindStart: (purpose: "login" | "receive" = "login") => req(`/api/market/bind/start${purpose === "receive" ? "?purpose=receive" : ""}`, { method: "POST" }, 20_000),
  marketBindStatus: (code: string, purpose: "login" | "receive" = "login") => req(`/api/market/bind/status?code=${encodeURIComponent(code)}${purpose === "receive" ? "&purpose=receive" : ""}`, {}, 15_000),
  marketReception: () => req("/api/market/reception"),
  marketSetReception: (enabled: boolean) => req("/api/market/reception", { method: "PATCH", body: JSON.stringify({ enabled }) }),
  marketRevokeReception: () => req("/api/market/reception", { method: "DELETE" }),
  marketUpload: (cardIds: string[]) => req("/api/market/upload", { method: "POST", body: JSON.stringify({ card_ids: cardIds }) }, 600_000),
  marketImport: (cardId: string) => req("/api/market/import", { method: "POST", body: JSON.stringify({ card_id: cardId }) }, 600_000),
  marketList: () => req("/api/market/cards", {}, 20_000),
  marketDrainImports: () => req("/api/market/imports/drain", { method: "POST" }, 180_000),
  marketLogin: async (email: string, password: string, remember_me = false) => {
    const result = await req("/api/market/login", { method: "POST",
      body: JSON.stringify({ email, password }) }, 45_000);
    try { localStorage.setItem("slice3.rememberEmail", email); localStorage.setItem("slice3.rememberLogin", remember_me ? "1" : "0"); } catch { /* 隐私模式忽略 */ }
    return result;
  },
  assetUrl: (url: string) => API + url,
};

// 浏览器会话由服务端 HttpOnly cookie 承载；令牌只在内存中保留，供开发测试的 Bearer 兼容路径使用。
let sessionToken: string | null = null;
let sessionEpoch = 0;

export function setSessionToken(token: string | null) {
  sessionEpoch += 1;
  sessionToken = token;
}

export function getSessionToken() {
  return sessionToken;
}

async function req(path: string, init: RequestInit = {}, timeoutMs = 0) {
  const epoch = sessionEpoch;
  const headers: Record<string, string> = {
    ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
    ...(init.headers as Record<string, string> | undefined),
  };
  if (sessionToken) headers["Authorization"] = `Bearer ${sessionToken}`;
  const timeout = timeoutMs ? new AbortController() : null;
  const timer = timeout ? window.setTimeout(() => timeout.abort(), timeoutMs) : undefined;
  const signal = timeout
    ? init.signal ? AbortSignal.any([init.signal, timeout.signal]) : timeout.signal
    : init.signal;
  try {
    const res = await fetch(`${API}${path}`, { ...init, signal, headers, credentials: "include" });
    const text = await res.text();
    if (epoch !== sessionEpoch) throw new Error("登录状态已变化，请重新加载。");
    let data: any = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!res.ok) {
      // First-launch binding runs before any account exists; its 401 is not a session expiry.
      const anonymousBound = path.startsWith("/api/auth/") || path.startsWith("/api/market/bind/");
      if (res.status === 401 && !anonymousBound) {
        setSessionToken(null);
        window.dispatchEvent(new Event("session-expired"));
      }
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
  } catch (error) {
    if (timeout?.signal.aborted && !init.signal?.aborted) {
      throw new Error("加载超时，请重试。");
    }
    throw error;
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
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
  memorySettings: () => req("/api/memory/settings"),
  memoryEngine: () => req("/api/memory/engine"),
  personalMemoryModels: () => req("/api/memory/models"),
  savePersonalMemoryModels: (body: any) => req("/api/memory/models", { method: "PUT", body: JSON.stringify(body) }),
  personalMemoryCatalog: (body: any, signal?: AbortSignal) => req("/api/memory/models/catalog", { method: "POST", body: JSON.stringify(body), signal }, 50_000),
  discoverPersonalMemoryReasoning: (body: {endpoint:string;model_name:string;api_key?:string;refresh?:boolean}, signal?: AbortSignal) =>
    req("/api/memory/models/reasoning", { method: "POST", body: JSON.stringify({
      endpoint:body.endpoint,model:body.model_name,
      ...(body.api_key?{api_key:body.api_key}:{}),...(body.refresh?{refresh:true}:{}),
    }), signal }, 55_000),
  testPersonalMemoryModel: (body: any) => req("/api/memory/models/test", { method: "POST", body: JSON.stringify(body) }, 60_000),
  rebuildPersonalMemory: () => req("/api/memory/models/rebuild", { method: "POST" }),
  disablePersonalMemoryModels: () => req("/api/memory/models", { method: "DELETE" }),
  saveMemoryEngine: (body: any) => req("/api/memory/engine", { method: "PUT", body: JSON.stringify(body) }),
  memoryModels: (body: any, signal?: AbortSignal) => req("/api/memory/engine/models", { method: "POST", body: JSON.stringify(body), signal }, 20_000),
  discoverModels: (body: any, signal?: AbortSignal) => req("/api/capabilities/models", { method: "POST", body: JSON.stringify(body), signal }, 20_000),
  memoryConfigure: (enabled: boolean, expected_revision: number) => req("/api/memory/settings",
    { method: "PATCH", body: JSON.stringify({ enabled, expected_revision }) }),
  memoryProfile: () => req("/api/memory/profile"),
  memorySources: () => req("/api/memory/sources"),
  memoryCharacter: (id: string) => req(`/api/memory/characters/${encodeURIComponent(id)}`),
  memoryForget: (id: string) => req(`/api/memory/facts/${encodeURIComponent(id)}`, { method: "DELETE" }),
  memoryCorrect: (id: string, content: string, expected_revision: number) => req(`/api/memory/facts/${encodeURIComponent(id)}`,
    { method: "PATCH", body: JSON.stringify({ content, expected_revision }) }),
  memoryReset: () => req("/api/memory/reset", { method: "POST" }),
  memoryFeedback: (id: string, body: unknown) => req(`/api/memory/characters/${encodeURIComponent(id)}/feedback`,
    { method: "POST", body: JSON.stringify(body) }),
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
  submitAssessment: (id: string, answers: Record<string, string | string[] | import("./lib/graphics").DrawingAnswer>, key: string) => req(`/api/assessments/${id}/attempts`, { method: "POST", body: JSON.stringify({ answers, idempotency_key: key }) }),
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
  answerAudio: (id: string) => req(`/api/teaching/${encodeURIComponent(id)}/answer-audio`, { method: "POST" }),
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
  characterBuilds: () => req("/api/character-builds"),
  characterBuildMaterial: (file:File) => {
    const form=new FormData();form.append("file",file);
    return req("/api/character-builds/material",{method:"POST",body:form},30_000);
  },
  createCharacterBuild: (body:Record<string,unknown>) => req("/api/character-builds",{method:"POST",body:JSON.stringify(body)}),
  characterBuild: (id:string) => req(`/api/character-builds/${id}`),
  streamCharacterBuild: async (id:string,onEvent:(data:any)=>void,signal:AbortSignal) => {
    const headers:Record<string,string>={Accept:"text/event-stream"};
    if(sessionToken)headers.Authorization=`Bearer ${sessionToken}`;
    const response=await fetch(`${API}/api/character-builds/${encodeURIComponent(id)}/content-events`,
      {headers,credentials:"include",signal});
    if(!response.ok||!response.body)throw new Error("人物内容流暂不可用");
    const reader=response.body.getReader(),decoder=new TextDecoder();
    let buffer="";
    try{
      while(!signal.aborted){
        const {value,done}=await reader.read();
        if(done)break;
        buffer+=decoder.decode(value,{stream:true});
        buffer=buffer.replace(/\r\n/g,"\n");
        let split:number;
        while((split=buffer.indexOf("\n\n"))>=0){
          const frame=buffer.slice(0,split);buffer=buffer.slice(split+2);
          const data=frame.split("\n").filter(line=>line.startsWith("data:"))
            .map(line=>line.slice(5).trimStart()).join("\n");
          if(!data)continue;
          const event=JSON.parse(data);
          onEvent(event);
          if(event.event==="done"||event.event==="error")return;
        }
      }
    }finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
  },
  characterBuildAction: (id:string,action:string,body:Record<string,unknown>={}) =>
    req(`/api/character-builds/${id}/${action}`,{method:"POST",body:JSON.stringify(body)}),
  savePersona: (id:string,expected_revision:number,persona:Record<string,unknown>) =>
    req(`/api/cards/${id}/persona`,{method:"PUT",body:JSON.stringify({expected_revision,persona})}),
  modelRoles: () => req("/api/capabilities/model-roles"),
  saveRoleConfiguration: (role:string,body:Record<string,unknown>) =>
    req(`/api/capabilities/model-roles/${role}/configuration`,{method:"PUT",body:JSON.stringify(body)}),
  revealContentKey: (configId:string) =>
    req(`/api/capabilities/configs/${encodeURIComponent(configId)}/credential/reveal`,{method:"POST"}),
  setModelRole: (role:string,config_id:string|null,expected_revision:number) =>
    req(`/api/capabilities/model-roles/${role}`,{method:"PUT",body:JSON.stringify({config_id,expected_revision})}),
  listConfigs: (kind?: string, signal?: AbortSignal) =>
    req(`/api/capabilities/configs${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`, { signal }, 10_000),
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
  describeReasoning: (body: { protocol: string; endpoint: string; model_name: string }, signal?: AbortSignal) =>
    req("/api/capabilities/reasoning/describe", { method: "POST", body: JSON.stringify(body), signal }),
  discoverReasoning: (body: {protocol:string;endpoint:string;model_name:string;api_key?:string;config_id?:string;config_version?:number;refresh?:boolean}, signal?:AbortSignal) =>
    req("/api/capabilities/reasoning/discover", {method:"POST",body:JSON.stringify(body),signal}, 55_000),
  testReasoning: (configId: string, configVersion: number, signal?: AbortSignal) =>
    req(`/api/capabilities/configs/${encodeURIComponent(configId)}/reasoning/test`, { method: "POST", body: JSON.stringify({ config_version: configVersion }), signal }),
  planningProcess: (taskId: string, signal?: AbortSignal) =>
    req(`/api/planning/tasks/${encodeURIComponent(taskId)}/process`, { signal }),
  listDisclosures: (taskId: string) =>
    req(`/api/disclosures?task_id=${encodeURIComponent(taskId)}`),
  confirmDisclosure: (body: Record<string, unknown>) =>
    req("/api/disclosures", { method: "POST", body: JSON.stringify(body) }),
  revokeDisclosure: (grantId: string) =>
    req(`/api/disclosures/${grantId}/revoke`, { method: "POST" }),
  createPairing: (deviceName: string) =>
    req("/api/connectors/pairing", { method: "POST", body: JSON.stringify({ device_name: deviceName }) }),
  listConnectors: (signal?: AbortSignal) => req("/api/connectors", { signal }, 10_000),
  revokeConnector: (bindingId: string) =>
    req(`/api/connectors/${bindingId}/revoke`, { method: "POST" }),
  taskSse: (taskId: string, lastEventId?: string, signal?: AbortSignal) => {
    const headers: Record<string, string> = {};
    if (sessionToken) headers["Authorization"] = `Bearer ${sessionToken}`;
    if (lastEventId) headers["Last-Event-ID"] = lastEventId;
    return fetch(`${API}/api/tasks/${taskId}/sse`, { headers, credentials: "include", signal });
  },

  listMaterials: () => req("/api/materials"),
  attachmentConfig: () => req("/api/attachments/config", {}, 10_000),
  preflightAttachment: (file: File, signal?: AbortSignal) => {
    const body = new FormData();
    body.append("file", file);
    return req("/api/attachments/preflight", { method: "POST", body, signal }, 300_000);
  },
  listAttachments: (signal?: AbortSignal) => req("/api/attachments", { signal }, 10_000),
  attachment: (id: string, signal?: AbortSignal) => req(`/api/attachments/${encodeURIComponent(id)}`, { signal }, 10_000),
  uploadAttachment: (file: File, config: any, key: string, signal?: AbortSignal) => {
    const body = new FormData();
      body.append("file", file);
      if (config) {
        body.append("config_id", config.config_id);
        body.append("config_version", String(config.config_version)); body.append("confirmed", "true");
        for (const role of ["semantic", "visual"]) {
          const target = config.extraction?.[role];
          if (target && config.extraction?.semantic) {
            body.append(`${role}_config_id`, target.config_id);
            body.append(`${role}_config_version`, String(target.config_version));
          }
        }
      }
    body.append("idempotency_key", key);
    return req("/api/attachments", { method: "POST", body, signal }, 300_000);
  },
  retryAttachment: (id: string, config: any) => req(`/api/attachments/${encodeURIComponent(id)}/retry`,
      { method: "POST", body: JSON.stringify(config ?
        { config_id: config.config_id, config_version: config.config_version, confirmed: true } : {}) }),
  removeAttachment: (id: string) => req(`/api/attachments/${encodeURIComponent(id)}`, { method: "DELETE" }),
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
  knowledge:(conversationId:string,after=0)=>req(`/api/teaching/${conversationId}/knowledge?after=${after}`),
  coverage:(conversationId:string,body:Record<string,unknown>)=>req(`/api/teaching/${conversationId}/coverage`,{method:"POST",body:JSON.stringify(body)}),
  completeKnowledge:(conversationId:string,body:Record<string,unknown>)=>req(`/api/teaching/${conversationId}/knowledge/complete`,{method:"POST",body:JSON.stringify(body)}),
  savePresentation:(conversationId:string,body:Record<string,unknown>)=>req(`/api/teaching/${conversationId}/presentation`,{method:"PUT",body:JSON.stringify(body)}),
  renameLesson:(conversationId:string,title:string,expected_title:string)=>req(`/api/teaching/${conversationId}/lesson-title`,{method:"PATCH",body:JSON.stringify({title,expected_title})}),
  feynmanAction:(conversationId:string,action:string,body:Record<string,unknown>={})=>
    req(`/api/teaching/${conversationId}/feynman/${action}`,{method:"POST",body:JSON.stringify(body)}),
  prewarmLibrary:()=>req('/api/teaching/prewarm-library',{method:'POST'}),
  firstPreparation:(courseId:string)=>req(`/api/courses/${courseId}/first-preparation`),
  upgradeLecture: (conversationId: string) => req(`/api/teaching/${conversationId}/upgrade`, {method:"POST"}),
  saveCheckpoint: (conversationId: string, body: Record<string, unknown>) =>
    req(`/api/teaching/${conversationId}/checkpoint`, { method: "POST", body: JSON.stringify(body) }),
  seekBackward: (conversationId:string,body:Record<string,unknown>) =>
    req(`/api/teaching/${conversationId}/seek-backward`,{method:"POST",body:JSON.stringify(body)}),
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
