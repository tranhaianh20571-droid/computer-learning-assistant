import { useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

const PREFERENCE = "lecture.textWidth.v3";
const DEFAULT_WIDTH = 380;
export default function LectureSplit({ children, pane }: { children: [ReactNode, ReactNode]; pane: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number>(() => {
    try { const value = Number(localStorage.getItem(PREFERENCE)); return value>=240 ? value : DEFAULT_WIDTH; }
    catch { return DEFAULT_WIDTH; }
  });
  const widthRef = useRef(width); widthRef.current = width;
  const limit = (value: number) => Math.max(240, Math.min(value, (ref.current?.clientWidth || 1200)*.45));
  const save = (value: number | null) => {
    setWidth(value ?? DEFAULT_WIDTH);
    try { value === null ? localStorage.removeItem(PREFERENCE) : localStorage.setItem(PREFERENCE, String(Math.round(value))); }
    catch { /* Restricted browser storage must not block learning. */ }
  };
  return <div ref={ref} className="lw-body" data-pane={pane} style={{ "--lecture-text-width": `min(${width}px, 45%)` } as CSSProperties}>
    {children[0]}
    <div className="lecture-divider" role="separator" tabIndex={0} aria-label="调整讲解栏宽度"
      aria-orientation="vertical" aria-valuemin={240} aria-valuenow={width || 240}
      title="拖动调整宽度，双击恢复默认"
      onDoubleClick={() => save(null)}
      onKeyDown={event => {
        if (event.key === "Home") { save(null); event.preventDefault(); }
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault(); const now = ref.current?.querySelector(".tx-col")?.getBoundingClientRect().width || 240;
        save(limit(now+(event.key==="ArrowLeft" ? 24 : -24)));
      }}
      onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault(); }}
      onPointerMove={event => {
        if (!event.currentTarget.hasPointerCapture(event.pointerId) || !ref.current) return;
        setWidth(limit(ref.current.getBoundingClientRect().right-event.clientX));
      }}
      onPointerUp={event => {
        if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
        event.currentTarget.releasePointerCapture(event.pointerId); save(widthRef.current);
      }} />
    {children[1]}
  </div>;
}
