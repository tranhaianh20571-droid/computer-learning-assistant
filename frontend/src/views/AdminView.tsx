import { useState } from "react";
import { api } from "../api";
import { errText } from "../lib/errors";
import Field from "../ui/Field";

/** 管理员审批页：通过 / 拒绝 / 停用，操作写入追加式审计。 */
export default function AdminView({ onErr, onOk }: any) {
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
