import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { useHashRoute } from "./hooks/useHashRoute";
import { useSession } from "./hooks/useSession";
import { AUTH_LEFT, LoginView, RegisterView, ResetView, VerifyView } from "./views/auth/AuthPages";
import AdminView from "./views/AdminView";
import CanvasView from "./views/CanvasView";
import TasksView from "./views/TasksView";
import CapabilityConfig from "./CapabilityConfig";
import PlanningView from "./PlanningView";
import ChatView from "./ChatView";
import TaskEntry from "./TaskEntry";
import { CourseOverview, RouteOverview } from "./Overview";
import MaterialsPage from "./MaterialsPage";
import LectureView from "./LectureView";
import "./tokens.css";

/**
 * 应用骨架（对齐设计规范 ds-1「工程制图 / 技术手册」）：
 * - 路由/会话状态在 hooks（useHashRoute / useSession），视图在 views/，共享原语在 lib/ 与 ui/；
 * - 登录/注册/验证/重置为独立整页路由（.auth 双栏，无侧栏）；
 * - 应用骨架为 .app 双栏：深色侧栏（分组导航 + 账户脚注）+ .main（顶栏面包屑 + .page）。
 */
export default function App() {
  const { me, setMe, booted } = useSession();
  const { route, navigate } = useHashRoute();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const go = useCallback((path: string) => {
    setError("");
    setNotice("");
    document.getElementById("side")?.classList.remove("open");
    navigate(path);
  }, [navigate]);

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

  // 登录/注册/验证/重置：独立整页路由（无侧栏）
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
            <TasksView me={me} onErr={setError} onOk={setNotice} onNeedLogin={() => go("/login")} />
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
