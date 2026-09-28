import { useCallback, useEffect, useRef, useState } from "react";
import { api, getSessionToken, setSessionToken } from "./api";
import CapabilityConfig from "./CapabilityConfig";
import PlanningView from "./PlanningView";
import ChatView from "./ChatView";
import TaskEntry from "./TaskEntry";
import { CourseOverview, RouteOverview } from "./Overview";
import MaterialsPage from "./MaterialsPage";
import LectureView from "./LectureView";
import "./tokens.css";

/**
 * 切片 3 UI（对齐设计规范 ds-1「工程制图 / 技术手册」）：
 * - 登录/注册/验证/重置为独立整页路由（.auth 双栏，无侧栏，参考 Google 登录式布局）；
 * - 应用骨架为 .app 双栏：深色侧栏（分组导航 + 账户脚注）+ .main（顶栏面包屑 + .page）；
 * - 错误码 → 用户可见模板文案（不显示原始异常）。
 */

const ERROR_TEXT: Record<string, string> = {
  AUTH_FAILED: "邮箱或密码不正确。",
  AUTH_LOCKED: "尝试过于频繁，请稍后再试。",
  AUTH_REJECTED: "无法完成注册，请检查邮箱格式或更换邮箱。",
  EMAIL_INVALID: "邮箱格式不正确。",
  PASSWORD_TOO_SHORT: "密码至少 15 个字符。",
  TOKEN_INVALID: "链接无效或已过期。",
  SESSION_INVALID: "登录状态已失效，请重新登录。",
  ACCESS_DENIED: "没有权限执行此操作。",
  STATE_INVALID: "当前状态不允许该操作。",
  USER_NOT_FOUND: "找不到该用户。",
  PROMPT_UNAVAILABLE: "提示词暂不可用，已跳过模型调用。",
  EVENTS_EXPIRED: "进度已过期，正在重新加载。",
  TASK_NOT_FOUND: "找不到该任务。",
  LEASE_LOST: "任务租约已过期，结果已忽略。",
  config_not_found: "找不到该配置。",
  credential_invalid: "凭据无效，请检查后重试。",
  capability_test_failed: "能力测试失败，请检查服务地址与凭据。",
  capability_unavailable: "该模型不支持所需能力，已停止调用。",
  connector_offline: "连接器离线，请重新连接。",
  connector_revoked: "连接器已撤销。",
  nonce_replay: "检测到重复请求，已拒绝。",
  disclosure_expired: "外发授权已过期，请重新确认。",
  disclosure_revoked: "外发授权已撤销。",
  disclosure_required: "缺少外发授权，已阻止外发。",
  pairing_code_invalid: "配对码无效或已使用。",
  invalid_target: "连接器只允许本机回环地址。",
  admin_only: "仅管理员可执行此操作。",
  MODEL_CAPABILITY_UNAVAILABLE: "该模型不支持所需能力，已停止调用。",
  LESSON_UNLOCK_REQUIRED: "未确认大纲，暂不能开始学习。",
  DRAFT_REVISION_MISMATCH: "草稿已在其他窗口更新，已加载服务端最新版本。",
  CONTEXT_BUDGET_EXCEEDED: "上下文超出预算，请缩小范围或补充资料。",
};

function errText(e: unknown): string {
  const code = (e as { code?: string })?.code;
  return (code && ERROR_TEXT[code]) || (e as Error)?.message || "操作失败";
}

type Me = { user_id: string; email: string; is_admin: boolean; status: string; display_name: string };

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中", leased: "处理中", running: "处理中", succeeded: "已完成", partial: "部分可用",
  failed: "失败", cancelled: "已取消", expired: "已过期",
  pending_email: "待验证邮箱", pending_approval: "待审批", approved: "已通过", rejected: "已拒绝", disabled: "已停用",
};

function Badge({ status }: { status: string }) {
  const cls = ["queued", "running", "succeeded", "partial", "failed", "cancelled"].includes(status) ? status : "queued";
  return <span className={`badge ${cls}`}>{STATUS_LABEL[status] || status}</span>;
}

const AUTH_LEFT = (
  <aside className="auth-l">
    <div className="brand"><span className="wm">学伴</span><span className="sub">CS · STUDY</span></div>
    <h1>一个邀请制的计算机学习空间</h1>
    <p>在这里定学习目标、生成路线与课程大纲，然后跟着短步骤的讲解一步步学。首版只对项目成员开放。</p>
    <ol className="how">
      <li><span className="n">01</span><span><b>提交申请</b> · 邮箱和一句长密码</span></li>
      <li><span className="n">02</span><span><b>验证邮箱</b> · 点开邮件里的链接</span></li>
      <li><span className="n">03</span><span><b>等待审批</b> · 由项目所有者人工处理</span></li>
      <li><span className="n">04</span><span><b>进入学习空间</b> · 两步都完成后才开放</span></li>
    </ol>
    <div className="foot">Chrome / Edge 最新版及上一个大版本</div>
  </aside>
);

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [route, setRoute] = useState<string>(() => location.hash.replace(/^#/, "") || "/home");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [booted, setBooted] = useState(false);

  const go = useCallback((path: string) => {
    setError("");
    setNotice("");
    document.getElementById("side")?.classList.remove("open");
    location.hash = path;
    setRoute(path);
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(location.hash.replace(/^#/, "") || "/home");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    (async () => {
      if (getSessionToken()) {
        try { setMe(await api.me()); } catch { setSessionToken(null); }
      }
      setBooted(true);
    })();
  }, []);

  // 移动端抽屉侧栏：点外部 / Escape / 导航跳转时收起
  // （hooks 必须位于早退 return 之前）
  const closeSide = useCallback(() => {
    document.getElementById("side")?.classList.remove("open");
  }, []);

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      const side = document.getElementById("side");
      if (!side || !side.classList.contains("open")) return;
      const target = e.target as HTMLElement;
      if (target.closest("#side")) return; // 侧栏内部点击不收起
      if (target.closest("[data-nav-toggle]")) return; // 开关按钮自身走 toggle
      side.classList.remove("open");
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeSide();
    };
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [closeSide]);

  // 登录/注册/验证/重置：独立整页路由（无侧栏），参考 Google 登录式布局
  const isAuthRoute = ["/login", "/register", "/verify", "/reset"].includes(route);
  if (!booted) return <div className="auth"><div className="auth-r"><p className="muted">加载中…</p></div></div>;

  if (isAuthRoute) {
    return (
      <div className="auth">
        {AUTH_LEFT}
        <div className="auth-r">
          <div className="auth-col">
            {error && <div className="error" role="alert">{error}</div>}
            {notice && <div className="notice" role="status">{notice}</div>}
            {route === "/register" && (
              <RegisterView onDone={(m: string) => { setNotice(m); setError(""); go("/verify"); }} onErr={setError} />
            )}
            {route === "/verify" && (
              <VerifyView onDone={(m: string) => { setNotice(m); setError(""); go("/login"); }} onErr={setError} />
            )}
            {route === "/login" && (
              <LoginView
                onDone={async (m: string) => {
                  setNotice(m); setError("");
                  try { setMe(await api.me()); } catch { /* ignore */ }
                  go("/home");
                }}
                onErr={setError}
              />
            )}
            {route === "/reset" && <ResetView onDone={setNotice} onErr={setError} />}
          </div>
        </div>
      </div>
    );
  }

  // ---------- 导航分组 ----------
  const navItem = (path: string, label: string) => (
    <button
      className="nav-a"
      aria-current={route === path ? "page" : undefined}
      onClick={() => go(path)}
    >
      {label}
    </button>
  );

  const CRUMBS: Record<string, string[]> = {
    "/home": ["新的学习任务"],
    "/chat": ["即时对话"],
    "/planning": ["课程规划"],
    "/materials": ["资料准备"],
    "/lecture": ["讲次教学"],
    "/tasks": ["任务状态"],
    "/admin": ["管理员审批"],
    "/capabilities": ["能力配置"],
  };
  const crumbHere = CRUMBS[route] || (route.startsWith("/route/") ? ["学习路线"] : route.startsWith("/course/") ? ["课程"] : []);
  const sideOpen = () => document.getElementById("side")?.classList.toggle("open");

  return (
    <div className="app">
      <nav className="side" id="side" aria-label="主导航">
        <div className="brand"><span className="wm">学伴</span><span className="sub">CS · STUDY</span></div>
        <div className="nav-g">
          <span className="gl">空间</span>
          {navItem("/home", "新的学习任务")}
          {navItem("/chat", "即时对话")}
          {navItem("/planning", "课程规划")}
        </div>
        <div className="nav-g">
          <span className="gl">学习</span>
          {navItem("/materials", "我的资料")}
          {navItem("/tasks", "任务状态")}
        </div>
        {me?.is_admin && (
          <div className="nav-g">
            <span className="gl">管理</span>
            {navItem("/admin", "管理员审批")}
          </div>
        )}
        {me && (
          <div className="nav-g">
            <span className="gl">配置</span>
            {navItem("/capabilities", "能力配置")}
          </div>
        )}
        <div className="side-foot">
          {me ? (
            <>
              <span className="who">{me.email}</span>
              <button
                onClick={async () => {
                  try { await api.logout(); } catch { /* ignore */ }
                  setMe(null);
                  go("/login");
                }}
              >
                退出登录
              </button>
            </>
          ) : (
            <>
              <span className="who">未登录</span>
              <button onClick={() => go("/login")}>登录</button>
              <button onClick={() => go("/register")}>申请加入</button>
            </>
          )}
        </div>
      </nav>
      <div className="main">
        <header className="top">
          <button className="iconbtn menu-btn" data-nav-toggle aria-label="打开导航" aria-controls="side" onClick={sideOpen}>☰</button>
          <nav className="crumb" aria-label="当前位置">
            <span className="here">{crumbHere[0] || "学伴"}</span>
          </nav>
        </header>
        <main className="page">
          {error && <div className="error" role="alert" style={{ marginBottom: "var(--s4)" }}>{error}</div>}
          {notice && <div className="notice" role="status" style={{ marginBottom: "var(--s4)" }}>{notice}</div>}

          {route === "/home" && (
            <TaskEntry me={me} onErr={setError} onOk={setNotice} go={go} />
          )}
          {route === "/materials" && me && (
            <MaterialsPage onErr={setError} onOk={setNotice} />
          )}
          {route === "/tasks" && (
            <HomeView me={me} onErr={setError} onOk={setNotice} onNeedLogin={() => go("/login")} />
          )}
          {route === "/admin" && me?.is_admin && <AdminView onErr={setError} onOk={setNotice} />}
          {route === "/canvas" && <CanvasView />}
          {route === "/capabilities" && me && (
            <CapabilityConfig isAdmin={me.is_admin} onErr={setError} onOk={setNotice} />
          )}
          {route === "/planning" && (
            <PlanningView onErr={setError} onOk={setNotice} needLogin={!me} go={go} />
          )}
          {route === "/chat" && (
            <ChatView onErr={setError} onOk={setNotice} needLogin={!me} />
          )}
          {route.startsWith("/route/") && (
            <RouteOverview routeId={route.slice("/route/".length)} onErr={setError} go={go} />
          )}
          {route.startsWith("/course/") && (
            <CourseOverview courseId={route.slice("/course/".length)} onErr={setError} onOk={setNotice} go={go} />
          )}
          {route.startsWith("/lecture/") && (
            <LectureView conversationId={route.slice("/lecture/".length)} onErr={setError} onOk={setNotice} go={go} />
          )}
        </main>
      </div>
    </div>
  );
}

function Field({ label, type, value, onChange, help, autoComplete }: any) {
  return (
    <div className="field">
      <label>{label}</label>
      <input
        className="inp"
        type={type}
        value={value}
        autoComplete={autoComplete}
        onChange={(e) => onChange(e.target.value)}
      />
      {help && <span className="help">{help}</span>}
    </div>
  );
}

function RegisterView({ onDone, onErr }: any) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  return (
    <form className="od-stack" style={{ ["--od-gap" as any]: "18px" }} onSubmit={(e) => e.preventDefault()}>
      <span className="kicker">申请加入</span>
      <h2 className="h1">注册账号</h2>
      <p className="muted">创建账户后需验证邮箱并等待管理员审批。</p>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} autoComplete="email" />
      <Field label="密码" type="password" value={password} onChange={setPassword} help="至少 15 个字符" autoComplete="new-password" />
      <Field label="显示名（可选）" type="text" value={name} onChange={setName} autoComplete="nickname" />
      <div className="row">
        <button className="btn btn-pri" type="submit"
          onClick={async () => {
            try {
              const r = await api.register(email, password, name);
              onDone(`注册成功。验证令牌：${r.verify_token}（演示环境直接展示，生产走邮件）`);
            } catch (e) { onErr(errText(e)); }
          }}
        >
          提交注册
        </button>
      </div>
      <span className="auth-switch">已有账号？<a href="#/login">登录</a></span>
    </form>
  );
}

function VerifyView({ onDone, onErr }: any) {
  const [token, setToken] = useState("");
  return (
    <div className="od-stack" style={{ ["--od-gap" as any]: "18px" }}>
      <span className="kicker">验证邮箱</span>
      <h2 className="h1">确认你的邮箱</h2>
      <ul className="track" aria-label="注册步骤">
        <li className="done"><span className="n">STEP 1</span><b>提交申请</b></li>
        <li className="now"><span className="n">STEP 2</span><b>验证邮箱</b></li>
        <li><span className="n">STEP 3</span><b>等待审批</b></li>
      </ul>
      <Field label="验证令牌" type="text" value={token} onChange={setToken} />
      <div className="row">
        <button className="btn btn-pri"
          onClick={async () => {
            try {
              const r = await api.verifyEmail(token);
              onDone(`邮箱已验证，当前状态：${STATUS_LABEL[r.status] || r.status}`);
            } catch (e) { onErr(errText(e)); }
          }}
        >
          验证
        </button>
        <span className="auth-switch">注册成功后令牌会显示在注册页提示里。</span>
      </div>
      <span className="auth-switch">已验证？<a href="#/login">去登录</a></span>
    </div>
  );
}

function LoginView({ onDone, onErr }: any) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  return (
    <form className="od-stack" style={{ ["--od-gap" as any]: "18px" }} onSubmit={(e) => e.preventDefault()}>
      <span className="kicker">登录</span>
      <h2 className="h1">回到你的学习空间</h2>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} autoComplete="email" />
      <Field label="密码" type="password" value={password} onChange={setPassword} autoComplete="current-password" />
      <div className="row" style={{ justifyContent: "flex-end", gap: "var(--s4)" }}>
        <button className="linkbtn" type="button" onClick={() => (location.hash = "/reset")}>找回密码</button>
        <button className="btn btn-pri" type="submit"
          onClick={async () => {
            try {
              await api.login(email, password);
              onDone("已登录");
            } catch (e) { onErr(errText(e)); }
          }}
        >
          登录
        </button>
      </div>
      <span className="auth-switch">还没有账号？<a href="#/register">申请加入</a></span>
    </form>
  );
}

function ResetView({ onDone, onErr }: any) {
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  return (
    <div className="od-stack" style={{ ["--od-gap" as any]: "18px" }}>
      <span className="kicker">找回密码</span>
      <h2 className="h1">通过邮件重设密码</h2>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} />
      <div className="row">
        <button className="btn"
          onClick={async () => {
            try {
              const r = await api.resetRequest(email);
              onDone(r.reset_token ? `重置令牌：${r.reset_token}` : "若邮箱存在将收到重置邮件。");
            } catch (e) { onErr(errText(e)); }
          }}
        >
          发送重置令牌
        </button>
      </div>
      <Field label="重置令牌" type="text" value={token} onChange={setToken} />
      <Field label="新密码" type="password" value={password} onChange={setPassword} help="至少 15 个字符" />
      <div className="row">
        <button className="btn btn-pri"
          onClick={async () => {
            try {
              await api.resetConfirm(token, password);
              onDone("密码已重置，旧会话已失效。");
            } catch (e) { onErr(errText(e)); }
          }}
        >
          确认重置
        </button>
      </div>
      <span className="auth-switch">想起密码了？<a href="#/login">去登录</a></span>
    </div>
  );
}

function AdminView({ onErr, onOk }: any) {
  const [userId, setUserId] = useState("");
  return (
    <div className="page-w">
      <div className="phead">
        <span className="kicker">管理员 · 账户状态</span>
        <div className="row"><h1 className="h1">注册审批</h1></div>
        <p className="lede">只展示已验证邮箱的申请。操作会写入追加式审计。</p>
      </div>
      <section className="card stack">
        <Field label="目标 user_id" type="text" value={userId} onChange={setUserId} />
        <div className="row">
          <button className="btn btn-pri"
            onClick={async () => {
              try { const r = await api.approve(userId); onOk(`已通过 ${r.user_id}`); }
              catch (e) { onErr(errText(e)); }
            }}
          >
            通过
          </button>
          <button className="btn danger"
            onClick={async () => {
              try { const r = await api.reject(userId); onOk(`已拒绝 ${r.user_id}`); }
              catch (e) { onErr(errText(e)); }
            }}
          >
            拒绝
          </button>
          <button className="btn danger"
            onClick={async () => {
              try { const r = await api.disable(userId); onOk(`已停用 ${r.user_id}`); }
              catch (e) { onErr(errText(e)); }
            }}
          >
            停用
          </button>
        </div>
      </section>
    </div>
  );
}

/** 任务状态（切片 0 演示页，保留模板化事件流） */
function HomeView({ me, onErr, onOk, onNeedLogin }: any) {
  const [taskId, setTaskId] = useState("");
  const [status, setStatus] = useState<any>(null);
  const [events, setEvents] = useState<any[]>([]);
  const esRef = useRef<AbortController | null>(null);

  const loadStatus = useCallback(async (id: string) => {
    try {
      const s = await api.taskStatus(id);
      setStatus(s);
    } catch (e) { onErr(errText(e)); }
  }, [onErr]);

  const openSse = useCallback(async (id: string) => {
    esRef.current?.abort();
    const ctrl = new AbortController();
    esRef.current = ctrl;
    try {
      const res = await api.taskSse(id);
      if (!res.ok || !res.body) {
        if (res.status === 410) {
          await loadStatus(id);
          return;
        }
        throw new Error("SSE failed");
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const part of parts) {
          const dataLine = part.split("\n").find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          try {
            const evt = JSON.parse(dataLine.slice(6));
            setEvents((prev) => [...prev, evt]);
            setStatus((s: any) =>
              s
                ? { ...s, status: evt.status, stage: evt.stage, revision: (s.revision || 0) }
                : { status: evt.status, stage: evt.stage }
            );
          } catch { /* ignore malformed */ }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") onErr(errText(e));
    }
  }, [loadStatus, onErr]);

  useEffect(() => () => esRef.current?.abort(), []);

  return (
    <div className="page-w">
      <div className="phead">
        <span className="kicker">任务</span>
        <div className="row"><h1 className="h1">任务状态</h1></div>
        <p className="lede">{me ? `已登录：${me.email}` : "未登录。请先登录后再创建任务。"}</p>
      </div>
      {!me && <button className="btn btn-pri" onClick={onNeedLogin}>去登录</button>}
      <section className="card stack">
        <div className="row">
          <button
            className="btn"
            disabled={!me}
            onClick={async () => {
              try {
                const r = await api.createTask({
                  idempotency_key: `web-${Date.now()}`,
                  kind: "generic",
                  subject_id: "demo_subject",
                  prompt_name: "lesson_step_v1",
                  allow_fallback: true,
                });
                setTaskId(r.task_id);
                onOk(r.prompt_binding_id ? `任务已创建 ${r.task_id}` : `任务已创建，但 ${r.prompt_unavailable || "提示词不可用"}`);
                await loadStatus(r.task_id);
                await openSse(r.task_id);
              } catch (e) { onErr(errText(e)); }
            }}
          >
            创建最简任务
          </button>
          <input
            className="inp"
            style={{ maxWidth: 260 }}
            placeholder="task_id"
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            aria-label="任务 ID"
          />
          <button className="btn" disabled={!taskId} onClick={() => loadStatus(taskId)}>刷新状态</button>
          <button className="btn" disabled={!taskId} onClick={() => openSse(taskId)}>订阅 SSE</button>
        </div>
      </section>

      {status && (
        <section className="card stack">
          <div className="row">
            <Badge status={status.status || "queued"} />
            <span className="idtag">stage={status.stage || "-"}</span>
            <span className="idtag">revision={status.revision ?? "-"}</span>
          </div>
          <span className="idtag">task_id={status.task_id || taskId}</span>
        </section>
      )}

      {events.length > 0 && (
        <section className="card stack">
          <h2 className="h3">事件（模板化）</h2>
          <div className="tscroll">
            <table className="table">
              <thead>
                <tr><th>序号</th><th>阶段</th><th>状态</th><th>错误码</th></tr>
              </thead>
              <tbody>
                {events.map((e, i) => (
                  <tr key={e.event_id || i}>
                    <td className="num">{e.sequence}</td>
                    <td>{e.stage}</td>
                    <td><Badge status={e.status} /></td>
                    <td className="idtag">{e.error_code || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

/** Excalidraw 最小只读场景（画布底座，不把坐标当业务顺序） */
function CanvasView() {
  const [ready, setReady] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await Promise.all([
          import("@excalidraw/excalidraw"),
          import("@excalidraw/excalidraw/index.css"),
        ]);
        if (!cancelled) setReady(true);
      } catch (e) {
        if (!cancelled) setErr("画布组件加载失败（离线或构建限制）。核心身份/任务流程不受影响。");
      }
    })();
    return () => { cancelled = true; };
  }, []);
  return (
    <div className="page-w">
      <div className="phead">
        <span className="kicker">底座</span>
        <h1 className="h1">画布底座</h1>
        <p className="lede">Excalidraw 仅作为只读场景画布；业务节点、排序和权限由服务端结构化数据驱动。</p>
      </div>
      {err && <div className="notice">{err}</div>}
      <div className="excalidraw-wrap" aria-label="板书区域，可滚动和缩放" style={{ padding: "1.5rem", color: "var(--ink3)" }}>
        {ready ? "Excalidraw 已加载（只读场景将在生成链路接入）。" : "加载画布组件…"}
      </div>
    </div>
  );
}
