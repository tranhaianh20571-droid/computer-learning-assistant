import { useState } from "react";
import { api } from "../../api";
import { errText, STATUS_LABEL } from "../../lib/errors";
import Field from "../../ui/Field";

/**
 * 认证整页（.auth 双栏，无侧栏，参考 Google 登录式布局）：
 * 注册 → 验证邮箱 → 登录 → 找回密码。onDone/onErr 由 App 注入；
 * 登录成功后由 App 恢复会话并跳转 /home。
 */

export const AUTH_LEFT = (
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

export function RegisterView({ onDone, onErr }: any) {
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

export function VerifyView({ onDone, onErr }: any) {
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

export function LoginView({ onDone, onErr }: any) {
  const [email, setEmail] = useState(() => localStorage.getItem("slice3.rememberEmail") || "");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(() => localStorage.getItem("slice3.rememberLogin") === "1");
  return (
    <form className="od-stack" style={{ ["--od-gap" as any]: "18px" }} onSubmit={(e) => e.preventDefault()}>
      <span className="kicker">登录</span>
      <h2 className="h1">回到你的学习空间</h2>
      <Field label="邮箱" type="email" value={email} onChange={setEmail} autoComplete="email" />
      <Field label="密码" type="password" value={password} onChange={setPassword} autoComplete="current-password" />
      <label className="checkline">
        <input type="checkbox" checked={rememberMe} onChange={(e) => setRememberMe(e.target.checked)} />
        <span>记住登录状态（本设备保留 14 天）</span>
      </label>
      <div className="row" style={{ justifyContent: "flex-end", gap: "var(--s4)" }}>
        <button className="linkbtn" type="button" onClick={() => (location.hash = "/reset")}>找回密码</button>
        <button className="btn btn-pri" type="submit"
          onClick={async () => {
            try {
              await api.login(email, password, rememberMe);
              // 只记住邮箱和偏好；密码与会话令牌永不落浏览器
              localStorage.setItem("slice3.rememberEmail", email);
              localStorage.setItem("slice3.rememberLogin", rememberMe ? "1" : "0");
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
