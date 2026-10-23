import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { CircleHelp } from "lucide-react";
import "./infoHint.css";

/**
 * 标题右上角的小问号：悬浮或聚焦时显示，点击固定；Esc 或点击别处关闭。
 * 只放简短说明，不放链接和可交互内容。
 */
export default function InfoHint({ label, children, size = 12 }: {
  label: string;
  children: ReactNode;
  size?: number;
}) {
  const id = useId();
  const root = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const tip = useRef<HTMLSpanElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const open = hovered || focused || pinned;

  useEffect(() => {
    if (!pinned) return;
    const dismiss = () => {
      setPinned(false);
      setHovered(false);
      setFocused(false);
      trigger.current?.blur();
    };
    const outside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!root.current?.contains(target) && !tip.current?.contains(target)) dismiss();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [pinned]);

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    const place = () => {
      const button = trigger.current?.getBoundingClientRect();
      if (!button) return;
      const heading = trigger.current?.closest("h2,h3,h4")?.getBoundingClientRect();
      const block = heading ?? button;
      const width = tip.current?.offsetWidth ?? 240;
      const height = tip.current?.offsetHeight ?? 0;
      const margin = 12;
      const left = Math.min(Math.max(margin, button.left - 4), Math.max(margin, innerWidth - width - margin));
      const below = block.bottom + 8;
      const top = below + height <= innerHeight - margin
        ? below
        : Math.max(margin, block.top - height - 8);
      setPosition({ left, top });
    };
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => { frame = 0; place(); });
    };
    place();
    addEventListener("resize", schedule);
    addEventListener("scroll", schedule, true);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      removeEventListener("resize", schedule);
      removeEventListener("scroll", schedule, true);
    };
  }, [open]);

  return <>
    <span className="info-hint" ref={root}
      onPointerEnter={event => { if (event.pointerType === "mouse") setHovered(true); }}
      onPointerLeave={event => { if (event.pointerType === "mouse") setHovered(false); }}>
      <button ref={trigger} type="button" className="info-hint-button"
        aria-label={`查看${label}说明`} aria-describedby={open ? id : undefined}
        onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
        onClick={() => setPinned(true)}>
        <CircleHelp size={size} aria-hidden="true" />
      </button>
    </span>
    {open && createPortal(
      <span ref={tip} id={id} role="tooltip" className="info-hint-tip"
        style={{ left: position?.left ?? 0, top: position?.top ?? 0, visibility: position ? "visible" : "hidden" }}>
        {children}
      </span>,
      document.body,
    )}
  </>;
}
