import { useState } from "react";
import { createRoot } from "react-dom/client";
import { Check, RefreshCw, Trash2, UserRound, GraduationCap } from "lucide-react";
import "../src/tokens.css";
import "../src/App.css";
import "../src/capabilities.css";
import "../src/revision.css";
import SettingsShell from "../src/ui/SettingsShell";
import Dialog from "../src/ui/Dialog";
import "./memory.css";

location.hash = "/capabilities?section=personal-memory";

function PersonalMemoryDesign() {
  const [enabled, setEnabled] = useState(true);
  const [cleared, setCleared] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [message, setMessage] = useState("");
  const [character, setCharacter] = useState("");
  return <SettingsShell name="我" go={() => {}} onLogout={() => {}}>
    <div className="capabilities-page">
      <div className="settings-body">
        <section className="personal-memory-settings memory-designed">
          <header className="memory-page-head">
            <h1>个人记忆</h1>
            <button type="button" className="btn memory-clear" data-memory="clear" onClick={() => setConfirm(true)}>
              <Trash2 aria-hidden="true" />清空个人记忆
            </button>
          </header>
          <section className="memory-enable-row" aria-labelledby="memory-switch-label">
            <div className="memory-enable-copy">
              <h2 id="memory-switch-label">启用记忆</h2>
              <p>允许自托管记忆服务及其配置的模型处理个人画像、课程记忆和人物表达反馈</p>
            </div>
            <div className="memory-switch-control">
              <span data-memory="switch-state">{enabled ? "已启用" : "已关闭"}</span>
              <button type="button" className="memory-switch" role="switch" aria-labelledby="memory-switch-label"
                aria-checked={enabled} data-memory="switch" onClick={() => {
                  setEnabled(!enabled); setMessage(enabled ? "记忆已关闭。" : "记忆已启用。");
                }}><span /></button>
            </div>
          </section>
          <p className="memory-existing-note">删除课程会清理该课程记忆，已形成的个人画像与人物经验保留，可在此单独遗忘。</p>
          <section className="memory-profile-section">
            <div className="memory-section-head">
              <h2>当前画像</h2>
              <button type="button" className="btn memory-refresh" data-memory="refresh" disabled={refreshing}
                onClick={() => {
                  setRefreshing(true);
                  setTimeout(() => { setRefreshing(false); setMessage("已刷新。"); }, 500);
                }}><RefreshCw aria-hidden="true"/><span>{refreshing ? "刷新中…" : "刷新"}</span></button>
            </div>
            <div className="memory-profile-sheet">
              <div data-memory="profile" hidden={cleared}>
                <article className="memory-profile-part">
                  <div className="memory-part-title"><span className="memory-part-icon"><UserRound aria-hidden="true" /></span><h3>用户身份</h3></div>
                  <p>现有记忆未记录用户的姓名、年龄、职业、学历或其他可确认的身份信息；这些内容未验证。</p>
                </article>
                <article className="memory-profile-part memory-profile-continuation">
                  <div className="memory-part-title"><span className="memory-part-icon"><GraduationCap aria-hidden="true" /></span><h3>背景基础</h3></div>
                </article>
              </div>
              <p className="memory-empty" data-memory="empty" hidden={!cleared}>暂无已整理的画像。</p>
            </div>
          </section>
          <section className="memory-feedback-section">
            <div className="memory-section-head"><h2>人物表达反馈</h2></div>
            <div className="memory-feedback-form">
              <select aria-label="人物" data-memory="character" value={character} onChange={event => setCharacter(event.target.value)}>
                <option value="">选择人物</option>
              </select>
              <select aria-label="表达方面" defaultValue="examples">
                <option value="examples">生活化举例</option><option value="rhythm">讲解节奏</option>
                <option value="wording">措辞</option><option value="interaction">互动</option>
                <option value="humor">幽默</option><option value="warmth">亲和与鼓励</option>
                <option value="persona">人物辨识度</option><option value="structure">讲解结构</option>
                <option value="detail">解释颗粒度</option><option value="terminology">术语使用</option>
                <option value="notation">公式与符号</option><option value="transitions">内容衔接</option>
                <option value="recap">总结回顾</option><option value="questioning">启发式提问</option>
              </select>
              <select aria-label="反馈" defaultValue="clearer">
                <option value="clearer">更清楚自然</option><option value="more">增加</option>
                <option value="less">减少</option><option value="keep">保持</option>
                <option value="slower">放慢一些</option><option value="faster">加快一些</option>
                <option value="simpler">更简单易懂</option><option value="deeper">更深入</option>
                <option value="warmer">更温和亲切</option><option value="more_direct">更直接简洁</option>
              </select>
              <button className="btn btn-pri" data-memory="save-feedback" disabled={!enabled || !character}>保存反馈</button>
            </div>
          </section>
          <div className="memory-operation-status" role="status"><Check aria-hidden="true" hidden={!message}/><span>{message}</span></div>
        </section>
      </div>
    </div>
    {confirm && <Dialog title="清空个人记忆？" kind="info" onClose={() => setConfirm(false)} actions={<>
      <button type="button" className="btn" autoFocus onClick={() => setConfirm(false)}>取消</button>
      <button type="button" className="btn btn-danger" onClick={() => {
        setCleared(true); setConfirm(false); setMessage("个人记忆已清空。");
      }}>确认清空</button>
    </>}>
      <p>清理全部画像、人物经验及课程记忆，保留课程和原始学习记录。</p>
    </Dialog>}
  </SettingsShell>;
}

createRoot(document.getElementById("root")!).render(<PersonalMemoryDesign />);
