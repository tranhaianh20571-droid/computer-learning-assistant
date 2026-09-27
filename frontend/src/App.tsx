import { useCallback, useEffect, useRef, useState } from "react";
import { api, getSessionToken, setSessionToken } from "./api";
import CapabilityConfig from "./CapabilityConfig";
import "./tokens.css";

/** 错误码 → 用户可见模板文案（不显示原始异常） */
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
};

function errText(e: unknown): string {
  const code = (e as { code?: string })?.code;
  return (code && ERROR_TEXT[code]) || (e as Error)?.message || "操作失败";
}

type Me = { user_id: string; email: string; is_admin: boolean; status: string; display_name: string };

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  leased: "处理中",
  running: "处理中",
  succeeded: "已完成",
  partial: "部分可用",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
  pending_email: "待验证邮箱",
  pending_approval: "待审批",
  approved: "已通过",
  rejected: "已拒绝",
  disabled: "已停用",
};

function Badge({ status }: { status: string }) {
  const cls = ["queued", "running", "succeeded", "partial", "failed", "cancelled"].includes(status)
    ? status
    : "queued";
  return <span className={`badge ${cls}`}>{STATUS_LABEL[status] || status}</span>;
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [route, setRoute] = useState<string>(() => location.hash.replace(/^#/, "") || "/home");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [booted, setBooted] = useState(false);

  const go = useCallback((path: string) => {
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

  if (!booted) return <div className="main">加载中…</div>;

  return (
    <div className="layout">
      <nav className="nav" aria-label="主导航">
        <div className="brand">学伴 · CS STUDY</div>
        <button className={route === "/home" ? "active" : ""} onClick={() => go("/home")}>任务状态</button>
        <button className={route === "/login" ? "active" : ""} onClick={() => go("/login")}>登录</button>
        <button className={route === "/register" ? "active" : ""} onClick={() => go("/register")}>注册</button>
        {me?.is_admin && (
          <button className={route === "/admin" ? "active" : ""} onClick={() => go("/admin")}>管理员审批</button>
        )}
        <button className={route === "/canvas" ? "active" : ""} onClick={() => go("/canvas")}>画布底座</button>
        {me && (
          <button className={route === "/capabilities" ? "active" : ""} onClick={() => go("/capabilities")}>能力配置</button>
        )}
        {me ? (
          <button
            onClick={async () => {
              try { await api.logout(); } catch { /* ignore */ }
              setMe(null);
              go("/login");
            }}
          >
            退出
          </button>
        ) : null}
      </nav>
      <main className="main">
        {error && <div className="error" role="alert">{error}</div>}
        {notice && <div className="notice" role="status">{notice}</div>}

        {route === "/register" && (
          <RegisterView
            onDone={(m: string) => { setNotice(m); setError(""); go("/login"); }}
            onErr={setError}
          />
        )}
        {route === "/verify" && (
          <VerifyView onDone={(m: string) => { setNotice(m); setError(""); go("/login"); }} onErr={setError} />
        )}
        {route === "/login" && (
          <LoginView
            onDone={async (m: string) => {
              setNotice(m);
              setError("");
              try { setMe(await api.me()); } catch { /* ignore */ }
              go("/home");
            }}
            onErr={setError}
          />
        )}
        {route === "/reset" && <ResetView onDone={setNotice} onErr={setError} />}
        {route === "/admin" && me?.is_admin && <AdminView onErr={setError} onOk={setNotice} />}
        {route === "/home" && (
          <HomeView me={me} onErr={setError} onOk={setNotice} onNeedLogin={() => go("/login")} />
        )}
        {route === "/canvas" && <CanvasView />}
        {route === "/capabilities" && me && (
          <CapabilityConfig isAdmin={me.is_admin} onErr={setError} onOk={setNotice} />
        )}
      </main>
    </div>
  );
}

function Field({ label, type, value, onChange, help, autoComplete }: any) {
  return (
    <div className="field">
      <label>{label}</label>
      <input
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
    <div className="card stack">
      <h1>注册</h1>
      <p className="lede">创建账户后需验证邮箱并等待管理员审批。</p>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} autoComplete="email" />
      <Field label="密码" type="password" value={password} onChange={setPassword}
        help="至少 15 个字符" autoComplete="new-password" />
      <Field label="显示名（可选）" type="text" value={name} onChange={setName} autoComplete="nickname" />
      <button
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
  );
}

function VerifyView({ onDone, onErr }: any) {
  const [token, setToken] = useState("");
  return (
    <div className="card stack">
      <h1>验证邮箱</h1>
      <Field label="验证令牌" type="text" value={token} onChange={setToken} />
      <button
        onClick={async () => {
          try {
            const r = await api.verifyEmail(token);
            onDone(`邮箱已验证，当前状态：${STATUS_LABEL[r.status] || r.status}`);
          } catch (e) { onErr(errText(e)); }
        }}
      >
        验证
      </button>
    </div>
  );
}

function LoginView({ onDone, onErr }: any) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  return (
    <div className="card stack">
      <h1>登录</h1>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} autoComplete="email" />
      <Field label="密码" type="password" value={password} onChange={setPassword} autoComplete="current-password" />
      <div className="row">
        <button
          onClick={async () => {
            try {
              await api.login(email, password);
              onDone("已登录");
            } catch (e) { onErr(errText(e)); }
          }}
        >
          登录
        </button>
        <button className="secondary" type="button" onClick={() => location.hash = "/reset"}>找回密码</button>
      </div>
    </div>
  );
}

function ResetView({ onDone, onErr }: any) {
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  return (
    <div className="card stack">
      <h1>重置密码</h1>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} />
      <button
        onClick={async () => {
          try {
            const r = await api.resetRequest(email);
            onDone(r.reset_token ? `重置令牌：${r.reset_token}` : "若邮箱存在将收到重置邮件。");
          } catch (e) { onErr(errText(e)); }
        }}
      >
        发送重置令牌
      </button>
      <Field label="重置令牌" type="text" value={token} onChange={setToken} />
      <Field label="新密码" type="password" value={password} onChange={setPassword} help="至少 15 个字符" />
      <button
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
  );
}

function AdminView({ onErr, onOk }: any) {
  const [userId, setUserId] = useState("");
  return (
    <div className="card stack">
      <h1>管理员审批</h1>
      <p className="lede">仅已验证邮箱的账户可进入待审批。操作会写入追加式审计。</p>
      <Field label="目标 user_id" type="text" value={userId} onChange={setUserId} />
      <div className="row">
        <button
          onClick={async () => {
            try { const r = await api.approve(userId); onOk(`已通过 ${r.user_id}`); }
            catch (e) { onErr(errText(e)); }
          }}
        >
          通过
        </button>
        <button className="secondary"
          onClick={async () => {
            try { const r = await api.reject(userId); onOk(`已拒绝 ${r.user_id}`); }
            catch (e) { onErr(errText(e)); }
          }}
        >
          拒绝
        </button>
        <button className="secondary"
          onClick={async () => {
            try { const r = await api.disable(userId); onOk(`已停用 ${r.user_id}`); }
            catch (e) { onErr(errText(e)); }
          }}
        >
          停用
        </button>
      </div>
    </div>
  );
}

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
            // 只消费模板化状态，不渲染原始 payload
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
    <div className="stack">
      <div className="card stack">
        <h1>任务状态</h1>
        <p className="lede">
          {me ? `已登录：${me.email}` : "未登录。请先登录后再创建任务。"}
        </p>
        {!me && <button onClick={onNeedLogin}>去登录</button>}
        <div className="row">
          <button
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
            style={{ minWidth: 260 }}
            placeholder="task_id"
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            aria-label="任务 ID"
          />
          <button className="secondary" disabled={!taskId} onClick={() => loadStatus(taskId)}>刷新状态</button>
          <button className="secondary" disabled={!taskId} onClick={() => openSse(taskId)}>订阅 SSE</button>
        </div>
      </div>

      {status && (
        <div className="card stack">
          <h2>当前状态</h2>
          <div className="row">
            <Badge status={status.status || "queued"} />
            <span className="mono">stage={status.stage || "-"}</span>
            <span className="mono">revision={status.revision ?? "-"}</span>
          </div>
          <div className="mono">task_id={status.task_id || taskId}</div>
        </div>
      )}

      {events.length > 0 && (
        <div className="card stack">
          <h2>事件（模板化）</h2>
          <table className="table">
            <thead>
              <tr><th>序号</th><th>阶段</th><th>状态</th><th>错误码</th></tr>
            </thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={e.event_id || i}>
                  <td className="mono">{e.sequence}</td>
                  <td>{e.stage}</td>
                  <td><Badge status={e.status} /></td>
                  <td className="mono">{e.error_code || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Excalidraw 最小只读场景（T07：画布底座，不把坐标当业务顺序） */
function CanvasView() {
  const [ready, setReady] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await import("@excalidraw/excalidraw");
        if (!cancelled) setReady(true);
      } catch (e) {
        if (!cancelled) setErr("画布组件加载失败（离线或构建限制）。核心身份/任务流程不受影响。");
      }
    })();
    return () => { cancelled = true; };
  }, []);
  return (
    <div className="card stack">
      <h1>画布底座</h1>
      <p className="lede">Excalidraw 仅作为只读场景画布；业务节点、排序和权限由服务端结构化数据驱动。</p>
      {err && <div className="notice">{err}</div>}
      <div className="excalidraw-wrap" aria-label="板书区域，可滚动和缩放">
        <div style={{ padding: "1.5rem", color: "#666" }}>
          {ready ? "Excalidraw 已加载（只读场景将在生成链路接入）。" : "加载画布组件…"}
          <ul>
            <li>最小场景：节点、箭头、自由绘制、缩放、撤销</li>
            <li>CJK 字体回退将在字体许可证锁定后启用</li>
          </ul>
        </div>
      </div>
    </div>
  );
}
