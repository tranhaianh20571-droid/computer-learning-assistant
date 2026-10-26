/** One client-side presentation clock. No per-character network/database work. */
import {orderedBoardBlocks} from "./boardGroups.ts";
import {alignedBoard} from "./boardAlignment.ts";
import type {BoardCue} from "./boardAlignment.ts";
export type Beat = { id: string; text: string; block_ids: string[]; highlight_ids?: string[]; unit_id?: string;cues?:BoardCue[];pdf_focus?:any };
export type BufferedTeaching = {
  text: string; shown: number; board: any; beats: Beat[]; epoch: string;
  final: boolean; active: boolean; credit: number; step_no: number; followup: boolean;
  delay: number; audioChars?: number; revealReady: boolean;
};
export class TeachingBuffer {
  entries = new Map<string, BufferedTeaching>();
  paused = false;
  frozen = new Map<string, any>();
  setPaused(paused: boolean) {
    if (paused === this.paused) return;
    if (paused) {
      for (const [key] of this.entries) this.frozen.set(key, this.view(key));
    } else this.frozen.clear();
    this.paused = paused;
  }
  receive(key: string, value: Partial<BufferedTeaching> & { text: string }, historical = false) {
    let entry = this.entries.get(key);
    const epoch = value.epoch || entry?.epoch || "saved";
    if (!entry || (value.epoch && epoch !== entry.epoch) || !value.text.startsWith(entry.text)) {
      entry = { text: "", shown: 0, board: null, beats: [], epoch,
        final: false, active: false, credit: 0, step_no: 0, followup: false, delay: 0, revealReady: true };
      this.entries.set(key, entry);
    }
    Object.assign(entry, value, { epoch });
    if (historical) entry.shown = Array.from(entry.text).length;
    // First character appears immediately, but a background prefetch stays hidden.
    if (entry.active && entry.revealReady && !this.paused && entry.shown === 0 && entry.text) entry.shown = 1;
    return entry;
  }
  advance(milliseconds: number) {
    let changed = false;
    for (const entry of this.entries.values()) {
      const total = Array.from(entry.text).length;
      if (!entry.active || !entry.revealReady || this.paused || entry.shown >= total) continue;
      if(entry.audioChars!==undefined){
        const next=Math.min(total,Math.max(entry.shown,entry.audioChars));
        if(next!==entry.shown){entry.shown=next;changed=true;}
        continue;
      }
      const elapsed=Math.min(milliseconds,100);
      if(entry.delay>0){entry.delay=Math.max(0,entry.delay-elapsed);continue;}
      entry.credit += elapsed * 12 / 1000;
      if(entry.credit>=1){
        const character=Array.from(entry.text)[entry.shown];
        entry.shown++;entry.credit=Math.min(1,entry.credit-1);changed=true;
        entry.delay=/[。！？.!?\n]/u.test(character)?360:/[，、；：,;:]/u.test(character)?140:0;
      }
    }
    return changed;
  }
  flush(key: string) {
    const entry = this.entries.get(key);
    if (entry) entry.shown = Array.from(entry.text).length;
    this.frozen.delete(key);
  }
  audioClock(key:string,chars:number|null){
    const entry=this.entries.get(key);
    if(entry)entry.audioChars=chars===null?undefined:Math.max(0,chars);
  }
  restoreCursor(key:string,chars:number){
    const entry=this.entries.get(key);
    if(!entry)return;
    entry.shown=Math.max(0,Math.min(Array.from(entry.text).length,chars));
    entry.credit=0;entry.delay=0;entry.audioChars=entry.shown;
    this.frozen.delete(key);
  }
  audioGate(key:string,blocked:boolean){
    const entry=this.entries.get(key);
    if(!entry)return;
    entry.revealReady=!blocked;
    if(blocked){entry.audioChars=entry.shown;entry.credit=0;entry.delay=0;}
  }
  view(key: string) {
    const entry = this.entries.get(key);
    if (!entry) return null;
    if (this.paused && this.frozen.has(key)) return this.frozen.get(key);
    const text = Array.from(entry.text).slice(0, entry.shown).join("");
    let pdfFocus=entry.board?.pdf_focus,beatOffset=0;
    for(const beat of entry.beats){
      if(entry.shown>beatOffset&&beat.pdf_focus)pdfFocus=beat.pdf_focus;
      beatOffset+=Array.from(beat.text).length;
    }
    if(pdfFocus&&entry.board?.source_reading){
      const base=(pdfFocus.regions||[]).filter((region:any)=>region.kind!=="emphasis");
      const cues=(entry.board.source_cues||[]).filter((cue:any)=>entry.shown>cue.start&&entry.shown<=cue.end);
      const figure=cues.find((cue:any)=>cue.pdf_focus);
      pdfFocus=figure?.pdf_focus||{...pdfFocus,regions:[...base,...cues.flatMap((cue:any)=>cue.regions||[])]};
    }
    const blocks = orderedBoardBlocks(entry.board?.presentation?.blocks || [],entry.beats);
    let visible = blocks, highlights: string[] = [];
    const progress = new Map<string, number>();
    let writingId = "", writingUnitId = "";
    let cueWriting:any=null;
    const revealGroup = (ids: string[], ratio: number, unitId: string) => {
      // block_ids are the model's semantic order. Do not reorder them by the
      // saved board layout: layout order is geometric, not teaching order.
      const members = ids.map(id => blocks.find((block: any) => block.id === id)).filter(Boolean) as any[];
      // Each declared block owns one semantic slice of the beat. A large graph
      // must not consume the whole beat merely because it has more geometry.
      const weights = members.map(() => 1);
      const total = weights.reduce((sum: number, weight: number) => sum + weight, 0);
      let budget = Math.max(0, Math.min(1, ratio)) * total;
      members.forEach((block: any, index: number) => {
        const part = Math.max(0, Math.min(1, budget / weights[index]));
        if (part > (progress.get(block.id) || 0)) {
          progress.set(block.id, part);
          writingId = block.id;
          writingUnitId = unitId;
        }
        budget -= weights[index];
      });
    };
    if(entry.board?.alignment_version===1){
      const aligned=alignedBoard(blocks,entry.beats,entry.shown);
      visible=aligned.visible;cueWriting=aligned.writing;
      writingId=cueWriting?.block_id||"";
      writingUnitId=cueWriting?.target||"";
      highlights=visible.flatMap((block:any)=>block.active_targets.map((target:string)=>`${block.id}:${target}`));
    }else if (entry.shown < Array.from(entry.text).length || !entry.final) {
      if (entry.beats.length) {
        let start = 0;
        for (const beat of entry.beats) {
          const length = Array.from(beat.text).length;
          if (entry.shown > start) {
            revealGroup(beat.block_ids, Math.min(1, (entry.shown - start) / Math.max(1, length)), beat.unit_id || beat.id);
            highlights = beat.highlight_ids || [];
          }
          start += length;
        }
      } else {
        const total = Math.max(1, Array.from(entry.text).length);
        revealGroup(blocks.map((block: any) => block.id), entry.shown / total, "legacy");
      }
      visible = blocks.filter((block: any) => (progress.get(block.id) || 0) > 0).map((block: any) => {
        const ratio = progress.get(block.id) || 0;
        if (block.type === "text") return { ...block, reveal_progress: ratio,
          text: Array.from(block.text).slice(0, Math.ceil(Array.from(block.text).length * ratio)).join("") };
        if (block.type === "code") return { ...block, reveal_progress: ratio,
          code: String(block.code).split("\n").slice(0, Math.ceil(String(block.code).split("\n").length * ratio)).join("\n") };
        return { ...block, reveal_progress: ratio };
      });
    }
    if (!writingId && entry.active && entry.shown > 0 && visible.length) {
      writingId = visible.at(-1)!.id;
      progress.set(writingId, 1);
    }
    return { ...entry, text, remaining: Math.max(0, Array.from(entry.text).length - entry.shown),
      board: entry.board ? { ...entry.board, beats: entry.beats, pdf_focus:pdfFocus,
        presentation: entry.board.presentation ? { ...entry.board.presentation, blocks: visible,
          all_blocks: blocks, highlight_ids: highlights,
          writing: writingId ? { block_id: writingId, unit_id: writingUnitId, progress: progress.get(writingId)??1,
            target:cueWriting?.target,epoch: entry.epoch } : null } : undefined } : null };
  }
}
