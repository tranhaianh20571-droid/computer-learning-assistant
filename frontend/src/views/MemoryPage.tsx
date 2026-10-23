import { useEffect, useRef, useState } from "react";
import { GraduationCap, Puzzle, RefreshCw, SlidersHorizontal, Trash2, UserRound } from "lucide-react";
import { api, characterApi } from "../api";
import { memoryProfileSections } from "../lib/memoryProfile";
import Dialog from "../ui/Dialog";
import "./memoryPage.css";

function ProfileContent({ content }: { content: string }) {
  return memoryProfileSections(content).map((section, index) => {
    const Icon = section.title === "背景基础" ? GraduationCap
      : section.title === "用户偏好" ? SlidersHorizontal
      : section.title === "能力与困难" ? Puzzle : UserRound;
    return <article className="memory-profile-part" key={index}>
      {section.title && <div className="memory-part-title">
        <span className="memory-part-icon"><Icon aria-hidden="true" /></span><h3>{section.title}</h3>
      </div>}
      {section.paragraphs.map((paragraph, i) => <p key={i}>{paragraph}</p>)}
    </article>;
  });
}

export default function MemoryPage({ onErr }: { onErr: (value: string) => void }) {
  const [settings, setSettings] = useState<any>(null);
  const [models, setModels] = useState<any[]>([]);
  const [cards, setCards] = useState<any[]>([]);
  const [card, setCard] = useState("");
  const [aspect, setAspect] = useState("examples");
  const [preference, setPreference] = useState("clearer");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [resetConfirm, setResetConfirm] = useState(false);
  const [resetError, setResetError] = useState("");
  const [characterModels, setCharacterModels] = useState<any[]>([]);
  const operationLock = useRef(false);
  const clearButton = useRef<HTMLButtonElement>(null);
  const confirmDialog = useRef<HTMLDivElement>(null);
  const resetWasOpen = useRef(false);
  useEffect(() => {
    if (resetConfirm) resetWasOpen.current = true;
    else if (!busy && resetWasOpen.current) {
      clearButton.current?.focus();
      resetWasOpen.current = false;
    }
  }, [resetConfirm, busy]);
  async function refresh() {
    const [s, model, character] = await Promise.all([
      api.memorySettings(), api.memoryProfile(),
      card ? api.memoryCharacter(card) : Promise.resolve(null),
    ]);
    setSettings(s);
    setModels(model.data?.items || model.data?.mental_models || []);
    if (character) setCharacterModels(character.data?.items || character.data?.mental_models || []);
    setNotice(model.status === "unavailable" ? "记忆服务暂不可用" : "");
  }
  useEffect(() => {
    let alive = true;
    Promise.all([api.memorySettings(), api.memoryProfile(), characterApi.list()])
      .then(([s, model, catalog]) => {
        if (!alive) return;
        setSettings(s);
        setModels(model.data?.items || model.data?.mental_models || []);
        setNotice(model.status === "unavailable" ? "记忆服务暂不可用" : "");
        setCards(catalog.data || catalog.items || []);
      }).catch(() => { if (alive) onErr("个人记忆暂时无法加载。"); });
    return () => { alive = false; };
  }, [onErr]);
  useEffect(() => {
    let alive = true;
    if (card) api.memoryCharacter(card).then(result => {
      if (alive) setCharacterModels(result.data?.items || result.data?.mental_models || []);
    }).catch(() => { if (alive) setNotice("该人物的经验暂不可用。"); });
    return () => { alive = false; };
  }, [card]);
  async function action(fn?: () => Promise<unknown>) {
    if (operationLock.current) return;
    operationLock.current = true;
    setBusy(true);
    try { if (fn) await fn(); await refresh(); }
    catch {
      if (resetConfirm && confirmDialog.current) setResetError("记忆操作未完成，请稍后重试。");
      else onErr("记忆操作未完成，请稍后重试。");
    }
    finally { operationLock.current = false; setBusy(false); }
  }
  function closeReset() {
    if (operationLock.current) return;
    setResetConfirm(false);
    clearButton.current?.focus();
  }
  return <section className="memory-designed">
    <header className="memory-page-head">
      <h1>个人记忆</h1>
      <button ref={clearButton} type="button" className="btn memory-clear" disabled={!settings || busy}
        onClick={() => { setResetError(""); setResetConfirm(true); }}><Trash2 aria-hidden="true" />清空个人记忆</button>
    </header>
    {!settings ? <p role="status">加载中…</p> : <>
      <section className="memory-enable-row" aria-labelledby="memory-switch-label">
        <div className="memory-enable-copy">
          <h2 id="memory-switch-label">启用记忆</h2>
          <p id="memory-switch-description">允许自托管记忆服务及其配置的模型处理个人画像、课程记忆和人物表达反馈</p>
        </div>
        <div className="memory-switch-control">
          <span>{settings.enabled ? "已启用" : "已关闭"}</span>
          <button type="button" className="memory-switch" role="switch" aria-checked={settings.enabled}
            aria-labelledby="memory-switch-label" aria-describedby="memory-switch-description" disabled={busy}
            onClick={() => action(() => api.memoryConfigure(!settings.enabled, settings.revision))}>
            <span aria-hidden="true" />
          </button>
        </div>
      </section>
      <p className="memory-existing-note">删除课程会清理该课程记忆，已形成的个人画像与人物经验保留，可在此单独遗忘。</p>
      {resetConfirm && <div ref={confirmDialog} onKeyDown={event => {
        if (event.key !== "Tab") return;
        const buttons = confirmDialog.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
        if (!buttons?.length) return;
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }}>
        <Dialog title="清空个人记忆？" kind="info" onClose={closeReset} actions={<>
          <button type="button" className="btn" autoFocus disabled={busy} onClick={closeReset}>取消</button>
          <button type="button" className="btn btn-danger" disabled={busy} onClick={() => action(async () => {
            setResetError("");
            await api.memoryReset();
            setCharacterModels([]);
            setResetConfirm(false);
            clearButton.current?.focus();
          })}>{busy ? "正在清空…" : "确认清空"}</button>
        </>}>
          <p>清理全部画像、人物经验及课程记忆，保留课程和原始学习记录。</p>
          {resetError && <p className="memory-reset-error" role="alert">{resetError}</p>}
        </Dialog>
      </div>}
      <section className="memory-profile-section" aria-labelledby="memory-profile-heading">
        <div className="memory-section-head">
          <h2 id="memory-profile-heading">当前画像</h2>
          <button type="button" className="btn memory-refresh" disabled={busy} onClick={() => action()}>
            <RefreshCw aria-hidden="true" />刷新
          </button>
        </div>
        <div className="memory-profile-sheet">
          {models.length ? models.map((m, index) => <div key={m.id || index}>
            <ProfileContent content={m.content || "正在整理"} />
          </div>) : <p className="memory-empty">暂无已整理的画像。</p>}
        </div>
      </section>
      <section className="memory-feedback-section" aria-labelledby="memory-feedback-heading">
        <div className="memory-section-head"><h2 id="memory-feedback-heading">人物表达反馈</h2></div>
        <div className="memory-feedback-form">
        <select aria-label="人物" value={card} disabled={busy} onChange={e => {
          setCharacterModels([]); setCard(e.target.value);
        }}>
          <option value="">选择人物</option>{cards.map(c =>
            <option key={c.card_id} value={c.card_id}>{c.profile?.name || c.card_id}</option>)}</select>
        <select aria-label="表达方面" value={aspect} disabled={busy} onChange={e => setAspect(e.target.value)}>
          <option value="examples">生活化举例</option><option value="rhythm">讲解节奏</option>
          <option value="wording">措辞</option><option value="interaction">互动</option><option value="humor">幽默</option>
          <option value="warmth">亲和与鼓励</option><option value="persona">人物辨识度</option>
          <option value="structure">讲解结构</option><option value="detail">解释颗粒度</option>
          <option value="terminology">术语使用</option><option value="notation">公式与符号</option>
          <option value="transitions">内容衔接</option><option value="recap">总结回顾</option>
          <option value="questioning">启发式提问</option></select>
        <select aria-label="反馈" value={preference} disabled={busy} onChange={e => setPreference(e.target.value)}>
          <option value="clearer">更清楚自然</option><option value="more">增加</option>
          <option value="less">减少</option><option value="keep">保持</option>
          <option value="slower">放慢一些</option><option value="faster">加快一些</option>
          <option value="simpler">更简单易懂</option><option value="deeper">更深入</option>
          <option value="warmer">更温和亲切</option><option value="more_direct">更直接简洁</option></select>
        <button className="btn btn-pri" disabled={busy || !card || !settings.enabled} onClick={() => action(() =>
          api.memoryFeedback(card, { card_revision: cards.find(c => c.card_id === card).revision,
            aspect, preference, idempotency_key: crypto.randomUUID() }))}>保存反馈</button>
        </div>
      {card && <div className="memory-character-experience">{characterModels.length ? characterModels.map((m, index) =>
        <ProfileContent key={m.id || index} content={m.content || "正在整理"} />)
        : <p className="muted">暂无该人物的已整理经验。</p>}</div>}
      </section>
      <p className="memory-operation-status" role="status">{notice}</p>
    </>}
  </section>;
}
