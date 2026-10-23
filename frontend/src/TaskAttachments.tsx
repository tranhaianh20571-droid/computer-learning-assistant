import { useEffect, useRef, useState } from "react";
import { Download, FileText, LoaderCircle, Paperclip, RotateCcw, X } from "lucide-react";
import { api } from "./api";
import Dialog from "./ui/Dialog";
import "./attachments.css";

export type Attachment = { material_id: string; status: string; kind?: string; name?: string;
  failure_code?: string; page_count?: number; file?: File; key?: string; processed_pages?: number;
  strategy?: string; batch_count?: number; completed_batches?: number; empty_pages?: number[];
  outline_count?: number; complex_table_pages?: number[]; figure_review_pages?: number[];
  figure_understanding_pages?: number[];
  phase?: string; extraction_destinations?: Record<string, any>;
  unit?: "page" | "slide" | "document"; local?: boolean; line_count?: number; failed_slide?: number;
  batches?: { start_page: number; end_page: number; status: string }[] };
const errors: Record<string, string> = {
  file_too_large: "文本最大 20 MiB，PPTX/Word 最大 200 MB，PDF 最大 512 MB。",
  too_many_pages: "PPTX 最多 300 张，PDF 最多 2000 页。",
  invalid_file_signature: "文件内容与格式不匹配，或文件已损坏。",
  pdf_encrypted: "请先解除 PDF 密码。",
  pdf_corrupt: "PDF 已损坏，无法读取。",
  unsupported_media_type: "支持 PDF、Word、PPTX、Markdown 和 TXT。",
  invalid_encoding: "请将文本保存为 UTF-8 编码后上传。",
  empty_file: "文件没有可读取内容。",
  text_too_large: "文本结构过多，请拆分资料。",
  LOCAL_PARSE_INVALID: "文本读取失败，请检查文件内容后重试。",
  PPTX_SLIDE_TOO_LARGE: "单张幻灯片及其资源超过 50 MB，请压缩该页后重试。",
  CONFIG_REQUIRED: "等待配置文档识别服务，文本资料已单独处理。",
  MODEL_CREDENTIAL_INVALID: "密钥或授权已变化，请检查设置后重试。",
  UPLOAD_QUOTA_EXCEEDED: "文档解析额度或存储配额不足。",
  OCR_PAGE_TIMEOUT: "解析超时，请稍后重试。",
  OCR_OUTPUT_INVALID: "解析未完成，请重试或更换文件。",
  MODEL_RATE_LIMITED: "识别服务繁忙，请稍后重试。",
  material_not_found: "资料已删除，请移除后重新上传。",
};
const keyFor = (owner: string) => `task-attachments:${owner}`;
const strategyLabel = (strategy?: string) => strategy === "local" ? "本地读取" :
  strategy === "batched" ? "分批完整识别" : "整份直接识别";
const localFile = (file: File) => /\.(txt|md|markdown)$/i.test(file.name);
const unitLabel = (item: { unit?: string }) => item.unit === "slide" ? "张幻灯片" : "页";
type PendingUpload = { files?: File[]; retry?: Attachment; config: any; previews?: any[] };
function attachmentStatus(item: Attachment) {
  if (item.status === "parsed") return item.local || item.unit === "document" ? "已读取" :
    `已识别${item.page_count ? ` · ${item.page_count} ${unitLabel(item)}` : ""}`;
  if (item.status === "uploading") return "上传中…";
  const progress = item.page_count ? `已识别 ${item.processed_pages || 0} / ${item.page_count} ${unitLabel(item)}` : "";
  if (item.status === "parsing") return item.phase === "extracting_questions" ? "题目整理中…" :
    item.local ? "读取中…" : progress || "识别中…";
  const message = errors[item.failure_code || ""] || "识别失败，请重试。";
  if (item.failure_code === "PPTX_SLIDE_TOO_LARGE" && item.failed_slide)
    return `第 ${item.failed_slide} 张幻灯片及其资源超过 50 MB，请压缩后重试。`;
  return progress && item.processed_pages ? `${progress} · ${message}` : message;
}

export default function TaskAttachments({ ownerId, items, onChange, disabled, onErr }: {
  ownerId: string; items: Attachment[]; onChange: React.Dispatch<React.SetStateAction<Attachment[]>>;
  disabled: boolean; onErr: (message: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  const actionLock = useRef(false);
  const [working, setWorking] = useState(false);
  const [pending, setPending] = useState<PendingUpload | null>(null);
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const details = items.find(item => item.material_id === detailsId);
  const [checking, setChecking] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [missing, setMissing] = useState(false);
  const current = useRef(items);
  const processing = items.some(item => item.status === "parsing");
  useEffect(() => { current.current = items; }, [items]);
  useEffect(() => {
    alive.current = true;
    let ids: string[] = [];
    try { ids = JSON.parse(sessionStorage.getItem(keyFor(ownerId)) || "[]"); } catch { /* no selection */ }
    onChange(ids.map(material_id => ({ material_id, status: "parsing" })));
    const controller = new AbortController();
    if (ids.length) api.listAttachments(controller.signal).then(result => {
      if (alive.current) onChange((result.data || []).filter((item: Attachment) => ids.includes(item.material_id)));
    }).catch(() => { if (alive.current) onErr("附件状态加载失败，请重新附上资料。"); });
    return () => { alive.current = false; controller.abort(); };
  }, [ownerId, onChange, onErr]);
  useEffect(() => {
    // IDs only; document names, bytes and credentials stay out of browser persistence.
    if (items.length) sessionStorage.setItem(keyFor(ownerId), JSON.stringify(items.filter(item => !item.file).map(item => item.material_id)));
  }, [items, ownerId]);
  useEffect(() => {
    if (!processing) return;
    const controller = new AbortController();
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try {
        for (const item of current.current.filter(value => value.status === "parsing")) {
          try {
            const next = await api.attachment(item.material_id, controller.signal);
            if (!controller.signal.aborted) onChange(previous => previous.map(old => old.material_id === item.material_id ? { ...old, ...next } : old));
          } catch (error: any) {
            if (!controller.signal.aborted && error.status === 404) onChange(previous => previous.map(old =>
              old.material_id === item.material_id ? { ...old, status: "failed", failure_code: "material_not_found" } : old));
          }
        }
      } catch { /* retain visible pending state; next poll can recover */ }
      finally { running = false; }
    };
    const timer = window.setInterval(poll, 3000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [processing, onChange]);

  async function prepare(files?: File[], retry?: Attachment) {
    if (actionLock.current) return;
    actionLock.current = true; setWorking(true);
    try {
      if (files) {
        if (items.length + files.length > 10) return onErr("每次最多附上 10 份资料。");
        if (files.some(file => !/\.(pdf|doc|docx|pptx|txt|md|markdown)$/i.test(file.name))) return onErr(errors.unsupported_media_type);
        if (files.some(file => file.size > (localFile(file) ? 20 * 1024 * 1024 :
          /\.pdf$/i.test(file.name) ? 512_000_000 : 200_000_000)))
          return onErr(errors.file_too_large);
      }
      const result = await api.attachmentConfig();
      if (retry && !retry.file && result.config)
        result.config = { ...result.config, extraction: retry.extraction_destinations || {} };
      if (!alive.current) return;
      let previews: any[] | undefined;
      if (files || retry?.file) {
        setChecking(true);
        previews = [];
        for (const file of files || [retry!.file!]) {
          previews.push(await api.preflightAttachment(file));
        }
      }
      const local = retry ? !!retry.local || !!retry.file && localFile(retry.file) : files?.every(localFile);
      if (alive.current) {
        if (local) {
          actionLock.current = false;
          await upload({ files, retry, config: null, previews });
        } else if (!result.config) {
          if (files) {
            const texts = files.filter(localFile);
            const waiting = files.filter(file => !localFile(file)).map(file => ({
              material_id: `local-${crypto.randomUUID()}`, name: file.name, file,
              key: crypto.randomUUID(), status: "failed", failure_code: "CONFIG_REQUIRED",
            }));
            onChange(previous => [...previous, ...waiting]);
            if (texts.length) {
              actionLock.current = false;
              await upload({ files: texts, config: null });
            }
          }
          setMissing(true);
        } else { setConfirmed(false); setPending({ files, retry, config: result.config, previews }); }
      }
    } catch (error: any) {
      if (alive.current) onErr(errors[error.code] || "资料预检未完成，请重试。");
    }
    finally { actionLock.current = false; if (alive.current) { setWorking(false); setChecking(false); } }
  }
  async function upload(localRequest?: PendingUpload) {
    const request = localRequest || pending;
    if (!request || (!localRequest && !confirmed) || actionLock.current) return;
    actionLock.current = true; setWorking(true);
    setPending(null);
    try {
      if (request.retry && !request.retry.file) {
        const next = await api.retryAttachment(request.retry.material_id, request.config);
        if (alive.current) onChange(previous => previous.map(item => item.material_id === next.material_id ? { ...item, ...next } : item));
      } else {
        const uploads = request.retry?.file ? [request.retry] : (request.files || []).map(file => ({
          material_id: `local-${crypto.randomUUID()}`, name: file.name, file, key: crypto.randomUUID(), status: "uploading",
        }));
        if (!request.retry) onChange(previous => [...previous, ...uploads]);
        for (const item of uploads) {
          if (!alive.current) break;
          onChange(previous => previous.map(old => old.material_id === item.material_id ? { ...old, status: "uploading", failure_code: undefined } : old));
          try {
            const next = await api.uploadAttachment(item.file!, localFile(item.file!) ? null : request.config, item.key!);
            if (alive.current) onChange(previous => previous.map(old => old.material_id === item.material_id ?
              { ...next, name: item.name } : old));
          } catch (error: any) {
            if (alive.current) onChange(previous => previous.map(old => old.material_id === item.material_id ?
              { ...old, status: "failed", failure_code: error.code || "OCR_OUTPUT_INVALID" } : old));
          }
        }
      }
    } catch { if (alive.current) onErr("重试未完成，请刷新状态后再试。"); }
    finally { actionLock.current = false; if (alive.current) setWorking(false); }
  }
  async function remove(item: Attachment) {
    if (actionLock.current) return;
    actionLock.current = true; setWorking(true);
    try {
      if (!item.file) {
        try { await api.removeAttachment(item.material_id); }
        catch (error: any) { if (error.status !== 404) throw error; }
      }
      if (alive.current) onChange(previous => {
        const next = previous.filter(old => old.material_id !== item.material_id);
        sessionStorage.setItem(keyFor(ownerId), JSON.stringify(next.filter(old => !old.file).map(old => old.material_id)));
        return next;
      });
    } catch { if (alive.current) onErr("移除未完成，请重试。"); }
    finally { actionLock.current = false; if (alive.current) setWorking(false); }
  }
  return <div className="task-attachments">
    <input ref={input} className="sr" type="file" aria-label="选择 PDF 或 Word 资料" accept=".pdf,.doc,.docx,.pptx,.md,.markdown,.txt"
      multiple disabled={disabled || working} onChange={event => {
        const files = Array.from(event.target.files || []); event.target.value = "";
        if (files.length) void prepare(files);
      }} />
    <button className="attach-btn" type="button" disabled={disabled || working || items.length >= 10}
      onClick={() => input.current?.click()}><Paperclip size={16} aria-hidden="true" />{checking ? "正在检查资料…" : `附上资料${items.length ? `（${items.length}）` : "（可选）"}`}</button>
    {items.length > 0 && <ul className="attachment-list">
      {items.map(item => <li key={item.material_id}>
        {["parsing", "uploading"].includes(item.status) ? <LoaderCircle className="attachment-spinner" size={17} /> : <FileText size={17} />}
        <div className="attachment-content"><button type="button" className="attachment-name attachment-open"
          disabled={!!item.file} onClick={() => setDetailsId(item.material_id)} title="查看识别进度">
          {item.name || `${item.kind?.toUpperCase() || "文档"} 资料`}</button>
          <small className={item.status === "failed" ? "attachment-failed" : "muted"} role="status">
            {attachmentStatus(item)}</small>
          {!!item.empty_pages?.length && <small className="attachment-review">{item.empty_pages.length} {unitLabel(item)}未返回文字，建议核对原件</small>}
          {!!item.complex_table_pages?.length && <small className="attachment-review">{item.complex_table_pages.length} {unitLabel(item)}含复杂表格，建议核对原件</small>}
          {!!item.figure_review_pages?.length && <small className="attachment-review">{item.figure_review_pages.length} {unitLabel(item)}图片未返回图中文字，需核对原件</small>}
        </div>
        {item.status === "parsed" && <a className="iconbtn" title="下载识别原文" aria-label={`下载 ${item.name || "资料"} 的识别原文`}
          href={`/api/attachments/${encodeURIComponent(item.material_id)}/markdown`}
          download={item.kind === "txt" ? "attachment.txt" : "attachment.md"}><Download size={16} /></a>}
        {item.status === "failed" && item.failure_code !== "material_not_found" && <button className="iconbtn" title="重试识别" aria-label={`重试 ${item.name || "资料"}`}
          disabled={working || disabled} onClick={() => prepare(undefined, item)}><RotateCcw size={16} /></button>}
        <button className="iconbtn" title="移除资料" aria-label={`移除 ${item.name || "资料"}`} disabled={working || disabled}
          onClick={() => remove(item)}><X size={16} /></button>
      </li>)}
    </ul>}
    {missing && <Dialog kind="info" title="设置文档识别" onClose={() => setMissing(false)} actions={<>
      <button className="btn" onClick={() => setMissing(false)}>取消</button>
      <a className="btn btn-pri" href="#/capabilities?section=documents">前往设置</a>
    </>}><p>PDF、Word 和 PPTX 需要你自己的 PaddleOCR API Key。TXT/Markdown 可直接本地读取。</p></Dialog>}
    {pending && <Dialog kind="info" title="确认识别资料" onClose={() => setPending(null)} actions={<>
      <button className="btn" onClick={() => setPending(null)}>取消</button>
      <button className="btn btn-pri" disabled={!confirmed || working} onClick={() => upload()}>上传并识别</button>
    </>}>
      <p>PDF、Word 和 PPTX 将发送至 PaddleOCR，可能扣减解析额度。大 PPTX 按原格式拆批，保留原始幻灯片编号。</p>
      <ul>{pending.files ? pending.files.map((file, i) => <li className="attachment-name" key={i}>{file.name}
        <small className="attachment-preview">{pending.previews?.[i]?.page_count ?
          `${pending.previews[i].page_count} ${unitLabel(pending.previews[i])} · 发送至 PaddleOCR · ` :
          localFile(file) ? "本地读取，不发送 OCR · " : "页数待识别 · "}
          {strategyLabel(pending.previews?.[i]?.strategy)}</small></li>) :
        <li className="attachment-name">{pending.retry?.name || "已上传资料"}
          <small className="attachment-preview">{pending.retry?.processed_pages ?
            `保留已完成的 ${pending.retry.processed_pages} ${unitLabel(pending.retry)}，继续未完成部分。` : "重新处理未完成部分。"}</small>
        </li>}</ul>
      <p className="help">服务：{pending.config.endpoint} · 配置 v{pending.config.config_version}</p>
      {!!pending.config.extraction?.semantic && <p className="help">
        题目整理：{pending.config.extraction.semantic.endpoint}
        {pending.config.extraction.visual && ` · 图形复核：${pending.config.extraction.visual.endpoint}`}
      </p>}
      <p className="help">本地读取仅指解析；后续向量检索和教学使用各自已授权的模型服务。</p>
      <label className="consent-choice"><input type="checkbox" checked={confirmed}
        onChange={event => setConfirmed(event.target.checked)} />我确认向上述服务发送资料进行识别与整理</label>
    </Dialog>}
    {details && <Dialog kind="info" title="资料识别进度" onClose={() => setDetailsId(null)}>
      <div className="attachment-details">
      <p className="attachment-name">{details.name || "已上传资料"}</p>
      <p role="status">{attachmentStatus(details)}</p>
      <p>{strategyLabel(details.strategy)}{details.batch_count ? ` · ${details.completed_batches || 0} / ${details.batch_count} 批完成` : ""}</p>
      {!!details.batches?.length && <ul className="attachment-batches">{details.batches.map((batch, i) =>
        <li key={i}>第 {batch.start_page}{batch.end_page ? `–${batch.end_page}` : ""} {unitLabel(details)}
          <span>{batch.status === "completed" ? "已完成" : batch.status === "processing" ? "识别中" :
            batch.status === "failed" ? "待重试" : "等待处理"}</span></li>)}</ul>}
      {!!details.empty_pages?.length && <p className="attachment-review">第 {details.empty_pages.join("、")} {unitLabel(details)}未返回文字，请核对原件。</p>}
      {!!details.complex_table_pages?.length && <p className="attachment-review">第 {details.complex_table_pages.join("、")} {unitLabel(details)}含复杂表格，请核对题干、公式和单元格顺序。</p>}
      {!!details.figure_review_pages?.length && <p className="attachment-review">第 {details.figure_review_pages.join("、")} {unitLabel(details)}图片未返回图中文字，图表内知识可能缺失。</p>}
      {!!details.figure_understanding_pages?.length && <p className="attachment-review">第 {details.figure_understanding_pages.join("、")} {unitLabel(details)}已返回图像理解；该内容是辅助解释，不是逐字 OCR。</p>}
      </div>
    </Dialog>}
  </div>;
}
