import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export function usePlanningAnswerDraft(taskId: string, ownerId: string, revision: number, initial: any[]) {
  const key = `planning.answers.${ownerId}.${taskId}`;
  const [answers, setAnswers] = useState<any[]>(() => {
    try {
      const cached = JSON.parse(localStorage.getItem(key) || "null");
      if (cached?.revision === revision && Array.isArray(cached.answers)) return cached.answers;
    } catch { /* Server snapshot remains authoritative. */ }
    return initial || [];
  });
  const [status, setStatus] = useState("");
  const version = useRef(revision);
  const pending = useRef<any[] | null>(null);
  const flight = useRef<Promise<void> | null>(null);
  const alive = useRef(true);
  const blocked = useRef(false);
  const latest = useRef(answers);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const flush = (): Promise<void> => {
    if (flight.current) return flight.current;
    flight.current = (async () => {
      while (pending.current && !blocked.current) {
        const value = pending.current;
        pending.current = null;
        try {
          const saved = await api.savePlanningAnswers(taskId, version.current, value);
          version.current = saved.revision;
          try { localStorage.setItem(key, JSON.stringify({ revision: saved.revision, answers: latest.current })); } catch { /* optional cache */ }
          if (alive.current) setStatus("回答草稿已保存");
        } catch (e: any) {
          blocked.current = true;
          if (alive.current) setStatus(e.code === "DRAFT_REVISION_MISMATCH"
            ? "回答已在其他页面更新，请重新进入规划核对。"
            : "暂未保存到服务器，回答已保留在此浏览器。");
          throw e;
        }
      }
    })().finally(() => { flight.current = null; });
    return flight.current;
  };
  const update = (value: any[]) => {
    latest.current = value;
    setAnswers(value);
    pending.current = value;
    try { localStorage.setItem(key, JSON.stringify({ revision: version.current, answers: value })); } catch { /* optional cache */ }
    setStatus("正在保存回答…");
    if (blocked.current) { setStatus("保存尚未恢复，请重新进入规划核对。"); return; }
    void flush().catch(() => {});
  };
  const beforeSubmit = async () => {
    if (blocked.current) throw new Error("请重新进入规划，核对保存的回答后再提交。");
    await flush();
    localStorage.removeItem(key);
  };
  return { answers, update, beforeSubmit, status };
}
