import { STATUS_LABEL } from "../lib/errors";

/** 任务状态徽章：仅映射已知的展示样式，未知状态按排队渲染。 */
export default function Badge({ status }: { status: string }) {
  const cls = ["queued", "running", "succeeded", "partial", "failed", "cancelled"].includes(status) ? status : "queued";
  return <span className={`badge ${cls}`}>{STATUS_LABEL[status] || status}</span>;
}
