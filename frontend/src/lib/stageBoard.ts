/** Restore the stage's board at a segment boundary without revealing future work. */
import {boardGroups} from "./boardGroups.ts";
export function stageBoard(previous: {step_no:number;board_scope?:string;board:any}[], current: any,
    currentNo: number, scope: string) {
  const ready = previous.filter(segment=>segment.step_no<currentNo && segment.board_scope===scope);
  const visible = new Map<string,any>(), all = new Map<string,any>(), refs=new Map<string,any>();
  const add=(scene:any, saved:boolean)=>{
    const blocks=scene?.presentation?.all_blocks||scene?.presentation?.blocks||[];
    const groups=boardGroups(blocks,scene?.beats||[]);
    const associations=new Map<string,string>();
    for(const group of groups){
      const previous=group.block_ids.map(id=>all.get(id)?.layout_group).find(Boolean);
      group.block_ids.forEach(id=>associations.set(id,previous||`${scope}:${group.id}`));
    }
    const grouped=(block:any)=>({...block,layout_group:all.get(block.id)?.layout_group||associations.get(block.id)});
    for(const block of blocks)all.set(block.id,grouped(block));
    for(const block of scene?.presentation?.blocks||[])visible.set(block.id,grouped(block));
    for(const ref of scene?.original_refs||[])refs.set(`${ref.material_id}:${ref.figure_id}`,ref);
    if(saved)for(const block of scene?.presentation?.blocks||[])all.set(block.id,grouped(block));
  };
  ready.forEach(segment=>add(segment.board,true));
  add(current,false);
  if(!current && !ready.length)return null;
  const base=current||ready.at(-1)?.board;
  return {...base,original_refs:[...refs.values()],presentation:{version:2,
    title:ready[0]?.board?.presentation?.title||base?.presentation?.title||"",
    blocks:[...visible.values()],all_blocks:[...all.values()],highlight_ids:current?.presentation?.highlight_ids||[],
    writing:current?.presentation?.writing||null}};
}

export function paragraphs(text:string, beats:{text:string}[] = []) {
  if(!text)return [];
  if(!beats.length)return text.split(/\n\s*\n/).filter(Boolean);
  const result:string[]=[];let remaining=Array.from(text).length;
  for(const beat of beats){
    const chars=Array.from(beat.text),count=Math.min(remaining,chars.length);
    if(count)result.push(chars.slice(0,count).join(""));
    remaining-=count;if(remaining<=0)break;
  }
  return result;
}
