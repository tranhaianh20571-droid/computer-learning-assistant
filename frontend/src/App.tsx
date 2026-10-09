import { Component, useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "./api";
import { useHashRoute } from "./hooks/useHashRoute";
import { loadGuest, useSession } from "./hooks/useSession";
import { AUTH_LEFT, LoginView, RegisterView, ResetView, VerifyView } from "./views/auth/AuthPages";
import AdminView from "./views/AdminView";
import CanvasView from "./views/CanvasView";
import CoursesPage from "./views/CoursesPage";
import TasksView from "./views/TasksView";
import CapabilityConfig from "./CapabilityConfig";
import Dialog from "./ui/Dialog";
import PlanningView from "./PlanningView";
import ChatView from "./ChatView";
import TaskEntry from "./TaskEntry";
import { CourseOverview, RouteOverview } from "./Overview";
import MaterialsPage from "./MaterialsPage";
import LectureView from "./LectureView";
import "./tokens.css";
import "./App.css";

const AUTH_DISABLED = import.meta.env.DEV && import.meta.env.VITE_DISABLE_AUTH === "1";

/**
 * 应用骨架（对齐设计规范 ds-1「工程制图 / 技术手册」，v2 布局收敛）：
 * - 侧栏三组：开始（新的学习任务）/ 学习（课程、课程规划、我的资料）/ 管理（注册审批+徽标）；
 *   任务状态与即时对话撤出主导航（路由保留：/tasks 由规划流程进入，/chat 由首页切换进入）；
 * - 侧栏可完全收起（桌面）：侧栏内「«」收起、顶栏「»」展开，偏好记入 localStorage；
 *   规划导图步骤可请求临时自动隐藏（离开该步骤自动恢复，不覆盖手动偏好）；
 * - 认证页与讲次教学工作区为独立整页（无侧栏）：讲次是全屏播放模式；
 * - 路由/会话状态在 hooks，视图在 views/，共享原语在 lib/ 与 ui/。
 */

/** 侧栏收起/展开按钮图形（双人字箭头，仅图形 + aria-label） */
function ChevronIcon({ dir }: { dir: "left" | "right" }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {dir === "left" ? (
        <><path d="M7 3.5 3 8l4 4.5" /><path d="M12.5 3.5 8.5 8l4 4.5" /></>
      ) : (
        <><path d="M9 3.5 13 8l-4 4.5" /><path d="M3.5 3.5 7.5 8l-4 4.5" /></>
      )}
    </svg>
  );
}

const SIDE_PREF_KEY = "ui.sideCollapsed";

/** 页面渲染错误兜底：任何页面崩溃只显示错误框，导航与顶栏仍可用（不整页白屏） */
class PageErrorBoundary extends Component<{ children: ReactNode }, { error: string }> {
  state = { error: "" };
  static getDerivedStateFromError(err: unknown) {
    return { error: String((err as any)?.message || err) };
  }
  render() {
    if (this.state.error) {
      return (
        <div className="note note-err" role="alert" style={{ margin: "24px" }}>
          <span className="tag">出错</span>
          <div className="tt">页面渲染失败：{this.state.error}</div>
          <div>导航仍然可用；重新加载通常可恢复。</div>
          <div className="acts">
            <button className="btn btn-pri btn-sm" onClick={() => location.reload()}>重新加载</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const { me, setMe, booted } = useSession();
  const { route, navigate } = useHashRoute();
  const [routePath, routeQuery = ""] = route.split("?");
  const query = new URLSearchParams(routeQuery);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pendingCount, setPendingCount] = useState(0);
  // 侧栏收起：手动偏好（持久化）+ 页面请求的临时隐藏（如规划导图步骤）
  const [sideCollapsed, setSideCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem(SIDE_PREF_KEY) === "1"; } catch { return false; }
  });
  const [sideAutoHidden, setSideAutoHidden] = useState(false);
  // 规划导图步骤：侧栏自动隐藏 + 顶栏转极简（只留菜单/展开按钮）
  const [mapMode, setMapMode] = useState(false);
  const sideHidden = sideCollapsed || sideAutoHidden;

  // 导图步骤进出：隐藏/恢复侧栏，顶栏同步转极简/还原
  const onMapMode = useCallback((on: boolean) => {
    setMapMode(on);
    setSideAutoHidden(on);
  }, []);

  // 换页清掉旧横幅，避免重进页面还挂着上一页的成功/失败提示
  useEffect(() => {
    setError("");
    setNotice("");
  }, [route]);

  // 导图步骤提示自动消失：浮层不占文档流，短暂停留后清除（成功提示足够）
  useEffect(() => {
    if (!mapMode || !notice) return;
    const t = window.setTimeout(() => setNotice(""), 4000);
    return () => window.clearTimeout(t);
  }, [mapMode, notice]);

  const go = useCallback((path: string) => {
    setError("");
    setNotice("");
    document.getElementById("side")?.classList.remove("open");
    navigate(path);
  }, [navigate]);

  // 管理员：待审批数徽标
  useEffect(() => {
    if (!me?.is_admin) { setPendingCount(0); return; }
    let alive = true;
    api.adminPending()
      .then((r: any) => { if (alive) setPendingCount((r.data || []).length); })
      .catch(() => {});
    return () => { alive = false; };
  }, [me?.is_admin, route]);

  // 移动端抽屉侧栏：点外部 / Escape / 导航跳转时收起（hooks 必须位于早退 return 之前）
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

  const isAuthRoute = ["/login", "/register", "/verify", "/reset"].includes(route);

  useEffect(() => {
    if (AUTH_DISABLED && isAuthRoute) navigate("/home");
  }, [isAuthRoute, navigate]);

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

  // 讲次教学工作区：全屏播放模式（无侧栏/顶栏骨架）
  if (route.startsWith("/lecture/")) {
    return (
      <PageErrorBoundary key={route}>
      <LectureView
        conversationId={route.slice("/lecture/".length)}
        onErr={setError}
        onOk={setNotice}
        go={go}
      />
      </PageErrorBoundary>
    );
  }

  // ---------- 导航分组 ----------
  const navItem = (path: string, label: string, badge?: number) => (
    <button
      className="nav-a"
      aria-current={route === path ? "page" : undefined}
      onClick={() => go(path)}
    >
      <span>{label}</span>
      {badge !== undefined && badge > 0 && <span className="nav-badge" aria-label={`待审批 ${badge} 项`}>{badge}</span>}
    </button>
  );

  const CRUMBS: Record<string, string[]> = {
    "/home": ["新的学习任务"],
    "/chat": ["即时对话"],
    "/planning": ["课程规划"],
    "/courses": ["课程"],
    "/materials": ["资料准备"],
    "/tasks": ["任务状态"],
    "/admin": ["管理员审批"],
    "/capabilities": ["设置"],
  };
  const crumbHere = CRUMBS[route] || (route.startsWith("/route/") ? ["学习路线"] : route.startsWith("/course/") ? ["课程"] : []);
  const sideOpen = () => document.getElementById("side")?.classList.toggle("open");
  // 切换侧栏收起：手动操作覆盖自动隐藏（导图步骤的临时隐藏由此解除）
  const toggleSide = () => {
    const next = !sideHidden;
    setSideAutoHidden(false);
    setSideCollapsed(next);
    try { localStorage.setItem(SIDE_PREF_KEY, next ? "1" : "0"); } catch { /* 隐私模式忽略 */ }
  };

  return (
    <div className={"app" + (sideHidden ? " side-collapsed" : "")}>
      <nav className="side" id="side" aria-label="主导航">
        <div className="brand">
          <span className="wm">学伴</span><span className="sub">CS · STUDY</span>
          <button
            className="iconbtn side-collapse-btn"
            onClick={toggleSide}
            aria-label="收起侧边栏"
            aria-expanded={!sideHidden}
            aria-controls="side"
            title="收起侧边栏"
          >
            <ChevronIcon dir="left" />
          </button>
        </div>
        <div className="nav-g">
          <span className="gl">开始</span>
          {navItem("/home", "新的学习任务")}
        </div>
        <div className="nav-g">
          <span className="gl">学习</span>
          {navItem("/courses", "课程")}
          {navItem("/planning", "课程规划")}
          {navItem("/materials", "我的资料")}
        </div>
        {me?.is_admin && (
          <div className="nav-g">
            <span className="gl">管理</span>
            {navItem("/admin", "注册审批", pendingCount)}
          </div>
        )}
        <div className="side-foot">
          {me && !me.is_guest ? (
            <>
              <button className="nav-a" onClick={() => go("/capabilities")}>设置</button>
              <span className="who">{me.email}</span>
              <button
                onClick={async () => {
                  try { await api.logout(); } catch { /* ignore */ }
                  // 退出后回到访客身份（访客 Cookie 未清除，之前的访客数据仍可访问）
                  setMe(await loadGuest());
                  go("/login");
                }}
              >
                退出登录
              </button>
            </>
          ) : (
            <>
              {me && <button className="nav-a" onClick={() => go("/capabilities")}>设置</button>}
              <span className="who">{me ? "访客模式" : "未登录"}</span>
              <button onClick={() => go("/login")}>登录</button>
              <button onClick={() => go("/register")}>申请加入</button>
            </>
          )}
        </div>
      </nav>
      <div className="main">
        <header className={"top" + (mapMode ? " top-min" : "")}>
          <button className="iconbtn menu-btn" data-nav-toggle aria-label="打开导航" aria-controls="side" onClick={sideOpen}>☰</button>
          {sideHidden && (
            <button
              className="iconbtn side-expand-btn"
              onClick={toggleSide}
              aria-label="展开侧边栏"
              aria-expanded={false}
              aria-controls="side"
              title="展开侧边栏"
            >
              <ChevronIcon dir="right" />
            </button>
          )}
          {!mapMode && (
            <nav className="crumb" aria-label="当前位置">
              <span className="here">{crumbHere[0] || "学伴"}</span>
            </nav>
          )}
        </header>
        <main className="page">
          <PageErrorBoundary key={route}>
          {error && (
            <Dialog title="出错了" onClose={() => setError("")}>{error}</Dialog>
          )}
          {notice && (mapMode ? (
            <div className="notice toast" role="status" onClick={() => setNotice("")} title="点击关闭">{notice}</div>
          ) : (
            <div className="notice" role="status" style={{ marginBottom: "var(--s4)" }}>{notice}</div>
          ))}

          {route === "/home" && (
            <TaskEntry me={me} onErr={setError} onOk={setNotice} go={go} />
          )}
          {route === "/courses" && (
            <CoursesPage me={me} onErr={setError} onOk={setNotice} go={go} />
          )}
          {route === "/materials" && me && (
            <MaterialsPage onErr={setError} onOk={setNotice} />
          )}
          {route === "/tasks" && (
            <TasksView me={me} onErr={setError} onOk={setNotice} onNeedLogin={() => go("/login")} />
          )}
          {route === "/admin" && me?.is_admin && (
            <AdminView onErr={setError} onOk={setNotice} onChanged={() => {
              api.adminPending().then((r: any) => setPendingCount((r.data || []).length)).catch(() => {});
            }} />
          )}
          {route === "/canvas" && <CanvasView />}
          {route === "/capabilities" && (
            me ? (
              <CapabilityConfig isAdmin={me.is_admin} onErr={setError} onOk={setNotice} />
            ) : (
              <div className="note note-info" role="status">
                <span className="tag">登录</span>
                <div className="tt">请先登录后管理能力配置</div>
                <div>模型、语音和连接器配置只对登录用户开放。</div>
                <div className="acts"><button className="btn btn-pri btn-sm" onClick={() => go("/login")}>去登录</button></div>
              </div>
            )
          )}
          {route === "/planning" && (
            <PlanningView key={me?.user_id || "anonymous"} ownerId={me?.user_id || ""} onErr={setError} onOk={setNotice} needLogin={!me} go={go} onMapMode={onMapMode} />
          )}
          {route === "/chat" && (
            <ChatView onErr={setError} onOk={setNotice} needLogin={!me} />
          )}
          {route.startsWith("/route/") && (
            <RouteOverview routeId={routePath.slice("/route/".length)} versionId={query.get("version_id") || undefined} search={query.get("q") || ""} onErr={setError} go={go} />
          )}
          {route.startsWith("/course/") && (
            <CourseOverview courseId={routePath.slice("/course/".length)} context={routeQuery} onErr={setError} onOk={setNotice} go={go} />
          )}
          </PageErrorBoundary>
        </main>
      </div>
    </div>
  );
}
