import { useEffect, useRef, useState } from "react";
import { TeachingBuffer } from "../lib/teachingBuffer";

export function useTeachingBuffer(conversation: string, ws: any, previews: Record<string, any>, current: number, playing = true, audioRequired = false, followupPlaying = false, playbackVersion=0) {
  const store = useRef(new TeachingBuffer());
  const initial = useRef(true);
  const [, render] = useState(0);
  useEffect(() => {
    store.current = new TeachingBuffer(); store.current.setPaused(!playing&&!followupPlaying); initial.current = true;
    render(n => n+1);
  }, [conversation,playbackVersion]);
  useEffect(() => { store.current.setPaused(!playing&&!followupPlaying); render(n => n+1); }, [playing,followupPlaying]);
  useEffect(() => {
    if (!ws || ws.conversation_id !== conversation) return;
    const valid = new Set<string>();
    for (const draft of Object.values(previews)) {
      if (!draft.followup && ws.steps?.some((step: any) => step.step_no === draft.step_no)) continue;
      if (draft.followup && ws.followups?.some((reply: any) => reply.role === "assistant" && reply.generation === draft.conversation_generation)) continue;
      const key = draft.followup ? `followup:${draft.conversation_generation}` : `step:${draft.step_no}`;
      valid.add(key);
      const draftValue: any = { text: draft.text || "", board: draft.board?{...draft.board,board_scope:draft.board_scope}:null, beats: draft.beats || [],
        epoch: `${draft.generation}:${draft.attempt}`, active: draft.followup ? followupPlaying : draft.step_no===current&&playing&&ws.session_state==="active",
        final: false, step_no: draft.step_no, followup: Boolean(draft.followup) };
      draftValue.revealReady = draft.followup ? false : !audioRequired || draft.step_no!==current;
      store.current.receive(key, draftValue);
    }
    for (const step of ws.steps || []) {
      const key = `step:${step.step_no}`; valid.add(key);
      const stepValue: any = { text: step.text, board: step.board, beats: step.board?.beats || [],
        active: step.step_no===current&&playing&&ws.session_state==="active", final: true, step_no: step.step_no, followup: false };
      if (!audioRequired || step.step_no!==current || !store.current.entries.has(key))
        stepValue.revealReady = !audioRequired || step.step_no!==current;
      store.current.receive(key, stepValue, initial.current && step.step_no<current);
      if (initial.current && step.step_no===current) {
        const saved=ws.last_checkpoint?.presentation;
        if (!audioRequired && typeof saved?.visible_chars === "number") {
          store.current.entries.get(key)!.shown=Math.min(Array.from(step.text).length,saved.visible_chars);
        }
      }
    }
    for (const reply of ws.followups || []) {
      if (reply.role !== "assistant") continue;
      const key = `followup:${reply.generation}`; valid.add(key);
      const board = reply.generation===ws.generation ? ws.followup_board : null;
      const latest=reply.generation===ws.generation&&ws.session_state==="awaiting_resume";
      const value:any={text:reply.content,board,beats:board?.beats||[],active:latest&&followupPlaying,
        final:true,followup:true,step_no:0};
      if(!latest||!ws.tts_authorized||!store.current.entries.has(key))value.revealReady=!latest||!ws.tts_authorized;
      store.current.receive(key,value,initial.current&&!latest);
      if(initial.current&&latest&&ws.channel_checkpoint){
        store.current.restoreCursor(key,ws.channel_checkpoint.visible_chars||0);
      }
    }
    for (const key of store.current.entries.keys()) {
      // Publication and workspace polling are separate requests. Keep the cursor
      // between the terminal snapshot and the saved step becoming visible.
      if (!valid.has(key) && ws.queued_tasks?.some((task:any)=>
        ["failed","expired"].includes(task.status) && key===`step:${task.step_no}`)) {
        store.current.entries.delete(key);store.current.frozen.delete(key);
      }
    }
    initial.current = false;
    render(n => n+1);
  }, [ws, previews, current, conversation, audioRequired,followupPlaying,playing]);
  useEffect(() => {
    let frame = 0, previous = 0;
    const tick = (now: number) => {
      const changed = store.current.advance(previous ? now-previous : 0);
      previous = now;
      if (changed) render(n => n+1);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [conversation]);
  return { view: (key: string) => store.current.view(key),
    pause: (paused: boolean) => {store.current.setPaused(paused);render(n=>n+1);},
    cursor: (key: string) => {
      const entry=store.current.entries.get(key);
      return {visible_chars:entry?.shown||0,text_complete:Boolean(entry?.final && entry.shown>=Array.from(entry.text).length)};
    },
    audioClock:(key:string,chars:number|null)=>store.current.audioClock(key,chars),
    restoreCursor:(key:string,chars:number)=>{store.current.restoreCursor(key,chars);render(n=>n+1);},
    audioGate:(key:string,blocked:boolean)=>{store.current.audioGate(key,blocked);render(n=>n+1);},
    flush: () => { for (const [key,entry] of store.current.entries) if (entry.active) store.current.flush(key); render(n => n+1); } };
}
