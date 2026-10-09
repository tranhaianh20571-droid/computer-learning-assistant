import { useEffect, type ReactNode } from "react";

/**
 * 弹窗原语（错误/提示）：fixed 遮罩脱离文档流，不挤占页面布局，
 * 因此只读导图等主内容不会因报错被推移或撑出滚动。
 * - Escape 或点击遮罩空白处关闭；
 * - actions 缺省时给一个「关闭」按钮；
 * - role 依据 kind 取 alertdialog / dialog。
 */
export default function Dialog({ title, children, onClose, kind = "error", actions }: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  kind?: "error" | "info";
  actions?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="modal-mask"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className={"modal" + (kind === "error" ? " modal-err" : "")}
        role={kind === "error" ? "alertdialog" : "dialog"}
        aria-modal="true"
        aria-label={title}
      >
        <div className="modal-head"><span className="modal-title">{title}</span></div>
        <div className="modal-body">{children}</div>
        <div className="modal-acts">
          {actions ?? <button className="btn btn-sm" onClick={onClose}>关闭</button>}
        </div>
      </div>
    </div>
  );
}
