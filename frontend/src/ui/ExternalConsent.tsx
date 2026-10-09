import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { canConfirmConsent, emptyConsent, type ConsentChoices, type ConsentRequest, type ConsentService } from "../lib/consent";
import Dialog from "./Dialog";

function ServiceDetails({ service }: { service: ConsentService }) {
  return <dl className="consent-service"><dt>模型</dt><dd>{service.model_name || service.service_kind || "已配置服务"}</dd>
    <dt>协议</dt><dd>{service.protocol || service.service_kind || "—"}</dd>
    <dt>服务地址</dt><dd>{service.service_endpoint || service.endpoint || "未提供"}</dd>
    <dt>配置版本</dt><dd>{service.config_version}</dd></dl>;
}
function ConsentDialog({ request, finish }: { request: ConsentRequest; finish: (value: ConsentChoices | null) => void }) {
  const [choices, setChoices] = useState<ConsentChoices>(() => ({ ...emptyConsent(), configId: request.content?.config_id || request.configs?.[0]?.config_id || "" }));
  const container = useRef<HTMLDivElement>(null);
  const service = request.configs?.length ? request.configs.find(item => item.config_id === choices.configId) : request.content;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = container.current?.closest('[role="dialog"]') as HTMLElement | null;
    dialog?.querySelector<HTMLElement>("button,input,a,select")?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !dialog) return;
      const nodes = [...dialog.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select,a[href]')];
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => { document.removeEventListener("keydown", trap); previous?.focus(); };
  }, []);
  return <Dialog kind="info" title={request.title} onClose={() => finish(null)} actions={<>
    <button className="btn btn-sm" onClick={() => finish(null)}>取消</button>
    {request.cover && <button className="btn btn-sm" onClick={() => finish({ ...emptyConsent(), procedural: true })}>生成本地封面</button>}
    {!service && <a className="btn btn-sm" href="#/capabilities" onClick={() => finish(null)}>配置模型</a>}
    <button className="btn btn-pri btn-sm" disabled={!canConfirmConsent(request, choices)} onClick={() => finish(choices)}>确认并继续</button>
  </>}><div ref={container} className="consent-body">
    <p><strong>{request.subject}</strong></p>
    {request.configs && request.configs.length > 1 && <label>图片模型<select className="inp" value={choices.configId} onChange={event => setChoices({ ...emptyConsent(), configId: event.target.value })}>
      {request.configs.map(config => <option key={config.config_id} value={config.config_id}>{config.model_name || config.config_id} · v{config.config_version}</option>)}</select></label>}
    {service ? <><ServiceDetails service={service} />
      <p>{request.cover ? "将向此图片服务发送当前主题及画面描述，可能产生费用。" : "将向此内容模型发送本讲课程上下文、学习目标及选定资料片段，可能产生费用。"}</p>
      {!request.cover && <p className="help">资料范围：{request.materials?.length ? `已选 ${request.materials.length} 份资料` : "不含上传资料"}。仅授权当前讲次和此配置版本。</p>}
      <label className="consent-choice"><input type="checkbox" checked={choices.content} onChange={event => setChoices({ ...choices, content: event.target.checked })} />我确认向上述服务发送{request.cover ? "当前主题和画面描述" : "本讲上下文与所列资料"}</label>
    </> : <p role="alert">尚未配置可用模型。不会发送内容，也不会启动生成。</p>}
    {request.tts && <><h3>可选语音</h3><ServiceDetails service={request.tts} />
      <label className="consent-choice"><input type="checkbox" checked={choices.tts} onChange={event => setChoices({ ...choices, tts: event.target.checked })} />我确认向此语音服务发送讲解文字并生成语音，可能产生费用</label></>}
    {!request.cover && <label className="consent-choice"><input type="checkbox" checked={choices.prewarm} onChange={event => setChoices({ ...choices, prewarm: event.target.checked })} />允许为当前讲次后台预生成内容{request.purpose === "prewarm" ? "（本次预生成必选）" : "（可选）"}</label>}
    {request.topics && request.topics.length > 0 && <><label className="consent-choice"><input type="checkbox" checked={choices.courses} onChange={event => setChoices({ ...choices, courses: event.target.checked })} />同时授权下列已确认课程使用此模型生成封面</label>
      <ul>{request.topics.map(topic => <li key={topic.title_hash}>{topic.title}</li>)}</ul></>}
    <p className="help">取消不会创建新的外发授权。主题、资料或配置变化后需重新确认。</p>
  </div></Dialog>;
}
let queue = Promise.resolve();
export function requestConsent(request: ConsentRequest): Promise<ConsentChoices | null> {
  let resolve!: (value: ConsentChoices | null) => void;
  const result = new Promise<ConsentChoices | null>(done => { resolve = done; });
  queue = queue.then(() => new Promise<void>(done => {
    const host = document.createElement("div"); document.body.append(host);
    const root = createRoot(host);
    let finished = false;
    const finish = (value: ConsentChoices | null) => {
      if (finished) return; finished = true; resolve(value);
      queueMicrotask(() => { root.unmount(); host.remove(); done(); });
    };
    root.render(<ConsentDialog request={request} finish={finish} />);
  }));
  return result;
}
