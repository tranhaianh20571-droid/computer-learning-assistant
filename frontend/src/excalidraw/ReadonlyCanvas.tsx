import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 切片 3 T06：只读课堂画布封装。
 * - 场景元素由服务端结构化数据受控转换（白名单），画布不承载业务操作；
 * - 缩放/平移/选择/复制文本为只读能力；业务操作走层级列表（T07 页面）；
 * - 「回到初始视角」按钮；加载失败降级为提示，不阻塞其他功能；
 * - CJK 字体回退由 Excalidraw 内置 Xiaolai 子集按需加载（T02 结论）。
 */

type SceneElement = Record<string, any>;

export default function ReadonlyCanvas({
  elements,
  height = 460,
  ariaLabel = "导图画布，可缩放、平移与复制文本",
}: {
  elements: SceneElement[];
  height?: number;
  ariaLabel?: string;
}) {
  const [err, setErr] = useState("");
  const [ready, setReady] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const excalidrawRef = useRef<any>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // 0.18 的样式不随 JS 注入，必须显式引入 index.css（含 CJK 字体回退声明）
        const [mod] = await Promise.all([
          import("@excalidraw/excalidraw"),
          import("@excalidraw/excalidraw/index.css"),
        ]);
        if (!cancelled) {
          (window as any).__excalidrawMod = mod;
          setReady(true);
        }
      } catch {
        if (!cancelled) setErr("画布组件加载失败（离线或构建限制），可改用下方层级列表。");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const CanvasComp = ready ? (window as any).__excalidrawMod?.Excalidraw : null;

  const initialData = useCallback(() => {
    const mod = (window as any).__excalidrawMod;
    if (!mod || !Array.isArray(elements)) return { elements: [] };
    try {
      // restore(data, localAppState, localElements, elementsConfig)
      // refreshDimensions：转换层不产出文本度量，交给画布用真实字体测量
      return mod.restore(
        { elements, appState: { viewBackgroundColor: "#ffffff" }, files: null },
        null,
        null,
        { repairBindings: false, refreshDimensions: true },
      );
    } catch {
      return { elements: [] };
    }
  }, [elements]);

  // 首次可用后保存 API 引用（回到初始视角）
  useEffect(() => {
    if (!ready || !excalidrawRef.current) return;
    const mod = (window as any).__excalidrawMod;
    const api = excalidrawRef.current;
    if (api && mod?.restore) {
      api.updateScene(initialData());
      api.scrollToContent(undefined, { fitToViewport: true });
    }
  }, [ready, initialData]);

  if (err) {
    return (
      <div className="excalidraw-wrap" role="status" style={{ minHeight: 120 }}>
        <div style={{ padding: "1rem", color: "#666" }}>{err}</div>
      </div>
    );
  }

  return (
    <div className="excalidraw-wrap" aria-label={ariaLabel}>
      <div
        ref={containerRef}
        style={{ height }}
        tabIndex={0}
        onKeyDown={(e) => {
          // 只读画布：拦截编辑类按键，避免误改（业务编辑走层级列表）
          if (["Delete", "Backspace", "Enter"].includes(e.key)) e.preventDefault();
        }}
      >
        {ready && CanvasComp ? (
          <CanvasComp
            ref={excalidrawRef}
            initialData={initialData()}
            viewModeEnabled={true}
            zenModeEnabled={false}
            gridModeEnabled={false}
            UIOptions={{ canvasActions: { export: false, saveToActiveFile: false, loadScene: false } }}
          />
        ) : (
          <div style={{ padding: "1rem", color: "#666" }}>加载画布组件…</div>
        )}
      </div>
      <div className="row" style={{ marginTop: "0.5rem" }}>
        <button
          className="secondary"
          type="button"
          onClick={() => {
            const api = excalidrawRef.current;
            if (api?.scrollToContent) api.scrollToContent(undefined, { fitToViewport: true });
            else if (api?.updateScene) api.updateScene(initialData());
          }}
        >
          回到初始视角
        </button>
        <span className="help">只读画布：拖动仅调整视角，不改变业务顺序。</span>
      </div>
    </div>
  );
}
