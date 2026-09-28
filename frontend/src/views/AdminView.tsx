import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { errText } from "../lib/errors";

/** 管理员审批队列：待审批列表 + 行内通过/拒绝/停用（操作写入追加式审计）。 */
export default function AdminView({ onErr, onOk, onChanged }: {
  onErr: (s: string) => void; onOk: (s: string) => void; onChanged?: () => void;
}) {
  const [rows, setRows] = useState<any[] | null>(null);
  const [busyId, setBusyId] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await api.adminPending();
      setRows(r.data || []);
    } catch (e) {
      onErr(errText(e));
      setRows([]);
    }
  }, [onErr]);

  useEffect(() => { load(); }, [load]);

  const act = useCallback(async (kind: "approve" | "reject", u: any) => {
    setBusyId(u.user_id);
    try {
      if (kind === "approve") await api.approve(u.user_id);
      else await api.reject(u.user_id);
      onOk(`已${kind === "approve" ? "通过" : "拒绝"} ${u.email}`);
      await load();
      onChanged?.();
    } catch (e) {
      onErr(errText(e));
    } finally {
      setBusyId("");
    }
  }, [load, onErr, onOk, onChanged]);

  return (
    <div className="page-w">
      <div className="phead">
        <span className="kicker">管理员 · 注册审批</span>
        <div className="row"><h1 className="h1">待审批申请</h1></div>
        <p className="lede">只展示已验证邮箱、等待审批的申请。操作会写入追加式审计。</p>
      </div>

      {rows === null && <p className="muted">加载中…</p>}
      {rows !== null && rows.length === 0 && (
        <div className="note note-info" role="status">
          <span className="tag">清空</span>
          <div className="tt">没有待审批的申请</div>
          <div>新申请在验证邮箱后会出现在这里。</div>
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <section className="card stack">
          {rows.map((u) => (
            <div className="row" key={u.user_id} style={{ alignItems: "center", gap: 14, flexWrap: "wrap" }}>
              <div style={{ minWidth: 220 }}>
                <b>{u.email}</b>
                {u.display_name && <span className="help" style={{ marginLeft: 8 }}>{u.display_name}</span>}
              </div>
              <span className="idtag">{u.user_id.slice(0, 14)}</span>
              {u.created_at && <span className="help">申请于 {u.created_at.slice(0, 10)}</span>}
              <span className="grow" />
              <button
                className="btn btn-pri"
                disabled={busyId === u.user_id}
                onClick={() => act("approve", u)}
              >
                通过
              </button>
              <button
                className="btn danger"
                disabled={busyId === u.user_id}
                onClick={() => act("reject", u)}
              >
                拒绝
              </button>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
