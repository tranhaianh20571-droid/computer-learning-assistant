import { useEffect, useState } from "react";

/** Excalidraw 最小只读场景演示页（画布底座，不把坐标当业务顺序）。 */
export default function CanvasView() {
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
