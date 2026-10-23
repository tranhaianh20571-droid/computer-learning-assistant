import { useEffect, useRef, useState } from "react";
import { api, characterApi } from "../../api";
import { errText } from "../../lib/errors";
import Field from "../../ui/Field";
import { openExternalPage, reserveExternalWindow } from "../../lib/externalWindow";

/**
 * 市集账号注册：账号只在市集网站创建。
 * 点「注册账号」打开市集，保留绑定上下文，完成注册后本机自动进入。
 */

type RegisterStatus = "idle" | "opening" | "waiting" | "blocked";

function useMarketRegister(onBound: () => void, onErr: (message: string) => void, purpose: "login" | "receive" = "login") {
  const [status, setStatus] = useState<RegisterStatus>("idle");
  const [url, setUrl] = useState("");
  const alive = useRef(true);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    // React 18 StrictMode mounts, cleans up, then mounts again. Without resetting
    // this flag the remount keeps alive=false forever, so the awaited result is
    // discarded and the page hangs on "正在准备注册页…".
    alive.current = true;
    return () => {
      alive.current = false;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  function poll(code: string, deadline: number) {
    timer.current = window.setTimeout(async () => {
      if (!alive.current) return;
      if (Date.now() >= deadline) {
        setStatus("idle");
        onErr(purpose === "receive" ? "授权连接已过期，请重新开启接收。" : "注册连接已过期，请重新点击注册账号。");
        return;
      }
      try {
        const result: any = await characterApi.marketBindStatus(code, purpose);
        if (!alive.current) return;
        if (result?.bound) {
          setStatus("idle");
          onBound();
          return;
        }
      } catch (error) {
        if (!alive.current) return;
        const code = (error as { code?: string }).code;
        if (code && code !== "MARKET_UNREACHABLE") {
          setStatus("idle");
          onErr("连接本机失败，请重新点击注册账号；已注册的账号也可直接登录。");
          return;
        }
      }
      poll(code, deadline);
    }, 2500);
  }

  function start() {
    if (status !== "idle") return;
    onErr("");
    setStatus("opening");
    // Open synchronously inside the click: window.open after an await is treated as
    // a non-user gesture and silently blocked by the browser, which looked like a hang.
    const popup = reserveExternalWindow();
    const fail = (message: string) => {
      try { popup?.close(); } catch { /* already closed */ }
      setStatus("idle");
      onErr(message);
    };
    void (async () => {
      try {
        const result: any = await characterApi.marketBindStart(purpose);
        if (!alive.current) { try { popup?.close(); } catch { /* ignore */ } return; }
        if (!result?.url || !result?.code) {
          fail("市集没有返回注册地址，请重试。");
          return;
        }
        setUrl(result.url);
        const opened = await openExternalPage(result.url, popup, false);
        if (!opened && window.desktopBrowser) { setStatus("idle"); setUrl(""); return; }
        setStatus(opened ? "waiting" : "blocked");
        poll(result.code, Date.now() + 10 * 60 * 1000);
      } catch (error) {
        if (!alive.current) { try { popup?.close(); } catch { /* ignore */ } return; }
        fail(errText(error));
      }
    })();
  }

  return { start, status, url };
}

function RegisterWaiting({ status, url }: { status: RegisterStatus; url: string }) {
  if (status === "idle") return null;
  if (status === "opening") return <p className="muted" role="status">正在打开市集…</p>;
  if (status === "blocked") {
    return <div className="od-stack" style={{ ["--od-gap" as any]: "10px" }} role="status">
      <p className="muted">浏览器拦截了新窗口，请手动打开市集：</p>
      <a className="btn btn-pri" href={url} target="_blank" rel="noreferrer">打开市集</a>
      <p className="muted">完成注册后会自动回到学伴。</p>
    </div>;
  }
  return <p className="muted" role="status">
    已打开市集，点击市集中的「注册账号」，完成后会自动进入学伴。
    {url && <> 没有打开？<a href={url} target="_blank" rel="noreferrer">点这里</a></>}
  </p>;
}

/** 深链接 #/register 的入口：直接给出打开注册页的按钮，不展示中间步骤页。 */
export function RegisterView({ onDone, onErr }: any) {
  const register = useMarketRegister(() => onDone("账号已创建并绑定这台电脑，正在进入学伴。"), onErr);
  return <div className="od-stack" style={{ ["--od-gap" as any]: "18px" }}>
    <span className="kicker">注册</span>
    <h2 className="h1">注册市集账号</h2>
    <p className="muted">账号在市集网站创建，完成后会自动绑定这台电脑。</p>
    {register.status === "idle"
      ? <div className="row"><button className="btn btn-pri" type="button" onClick={register.start}>打开市集</button></div>
      : <RegisterWaiting status={register.status} url={register.url} />}
    <span className="auth-switch">已有账号？<a href="#/login">登录</a></span>
  </div>;
}

export function MarketReceiveView({ onErr }: { onErr: (message: string) => void }) {
  const [done, setDone] = useState(false);
  const receive = useMarketRegister(() => setDone(true), onErr, "receive");
  return <div className="od-stack" style={{ ["--od-gap" as any]: "18px" }}>
    <span className="kicker">市集接收</span>
    <h2 className="h1">开启账号接收</h2>
    <p className="muted">在市集选择要接收的账号。卡片只会存入该账号的本地空间，不会切换本机登录账号。</p>
    {done ? <div role="status">
      <p>已开启后台接收，本机未登录或切换账号也可继续接收。</p>
      <a className="btn btn-pri" href={receive.url.split("#")[0]} target="_blank" rel="noreferrer">返回市集</a>
    </div> : <>
      <button className="btn btn-pri" type="button" disabled={receive.status !== "idle"} onClick={receive.start}>
        {receive.status === "idle" ? "开启账号接收" : "等待市集授权…"}
      </button>
      {receive.status !== "idle" && <p className="muted" role="status">
        请在市集完成账号授权。<a href={receive.url || undefined} target="_blank" rel="noreferrer">打开市集</a>
      </p>}
    </>}
    <a href="#/login">返回本机登录</a>
  </div>;
}

export function VerifyView({ onErr }: any) {
  return <div className="od-stack" style={{ ["--od-gap" as any]: "18px" }}>
    <span className="kicker">验证邮箱</span>
    <h2 className="h1">邮箱验证在市集完成</h2>
    <p className="muted">注册时会向你的邮箱发送 6 位验证码，在网页上填写即可。验证通过后账号会自动绑定这台电脑。</p>
    <div className="row">
      <button className="btn btn-pri" type="button" onClick={() => { onErr(""); location.hash = "/register"; }}>去注册</button>
      <a className="btn" href="#/login">去登录</a>
    </div>
  </div>;
}

export function LoginView({ onDone, onErr }: any) {
  const [email, setEmail] = useState(() => localStorage.getItem("slice3.rememberEmail") || "");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(() => localStorage.getItem("slice3.rememberLogin") === "1");
  const [busy, setBusy] = useState(false);
  const register = useMarketRegister(async () => {
    try { await onDone("账号已创建并绑定这台电脑，正在进入学伴。"); }
    catch { onErr("绑定已完成，但本机会话恢复失败，请点登录重试。"); }
  }, onErr);

  return (
    <form className="od-stack" style={{ ["--od-gap" as any]: "18px" }} onSubmit={async e => {
      e.preventDefault();
      if (busy) return;
      setBusy(true);
      try {
        await characterApi.marketLogin(email, password, rememberMe);
        localStorage.setItem("slice3.rememberEmail", email);
        localStorage.setItem("slice3.rememberLogin", rememberMe ? "1" : "0");
        await onDone("已登录");
      } catch (error) { onErr(errText(error)); }
      finally { setBusy(false); }
    }}>
      <span className="kicker">登录</span>
      <h2 className="h1">回到你的学习空间</h2>
      <p className="muted">让好奇落笔，让理解慢慢生长。</p>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} autoComplete="email" />
      <Field label="密码" type="password" value={password} onChange={setPassword} autoComplete="current-password" />
      <div className="auth-options"><label className="checkline">
        <input type="checkbox" checked={rememberMe} onChange={(e) => setRememberMe(e.target.checked)} />
        <span title="本设备保留 14 天">记住登录状态</span>
      </label>
        <button className="linkbtn" type="button" onClick={() => (location.hash = "/reset")}>找回密码</button>
      </div>
      <button className="btn btn-pri" type="submit" disabled={busy}>{busy ? "正在登录…" : "登录"}</button>
      <a className="linkbtn" href="#/market-receive">仅开启市集接收</a>
      <RegisterWaiting status={register.status} url={register.url} />
      <span className="auth-switch">还没有账号？
        <button className="linkbtn" type="button" onClick={() => void register.start()} disabled={register.status !== "idle"}>
          {register.status === "idle" ? "注册账号" : "等待注册…"}
        </button>
      </span>
    </form>
  );
}

export function ResetView({ onDone, onErr }: any) {
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
              await api.resetRequest(email);
              onDone("若邮箱存在，将收到重置邮件。");
            } catch (e) { onErr(errText(e)); }
          }}
        >
          发送重置令牌
        </button>
      </div>
      <Field label="重置令牌" type="text" value={token} onChange={setToken} />
      <Field label="新密码" type="password" value={password} onChange={setPassword} help="至少 8 个字符" />
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
