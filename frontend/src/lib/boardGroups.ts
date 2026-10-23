/** Local knowledge groups from existing semantic beats, with legacy adjacency fallback. */
export type BoardGroup = { id: string; block_ids: string[] };
export function orderedBoardBlocks<T extends {id:string;layout_group?:string}>(blocks:T[], beats:{block_ids?:string[];cues?:{block_id:string}[]}[]=[]):T[] {
  // Composed stages already carry the immutable order of earlier segments.
  if(blocks.some(block=>block.layout_group))return blocks;
  const byId=new Map(blocks.map(block=>[block.id,block])),ordered=new Map<string,T>();
  for(const beat of beats)for(const id of [...(beat.cues||[]).map(cue=>cue.block_id),...(beat.block_ids||[])]){
    const block=byId.get(id);if(block&&!ordered.has(id))ordered.set(id,block);
  }
  for(const block of blocks)if(!ordered.has(block.id))ordered.set(block.id,block);
  return [...ordered.values()];
}
export function boardGroups(blocks: any[], beats: {block_ids?:string[]}[] = []): BoardGroup[] {
  blocks=orderedBoardBlocks(blocks,beats);
  const ids=new Set(blocks.map(block=>block.id));
  const parent=new Map<string,string>(blocks.map(block=>[block.id,block.id]));
  const root=(id:string):string=>{const p=parent.get(id)||id;return p===id?id:root(p);};
  for(const beat of beats){
    const members=(beat.block_ids||[]).filter(id=>ids.has(id));
    for(const id of members.slice(1))parent.set(root(id),root(members[0]));
  }
  const groupedBeats=new Set(beats.flatMap(beat=>beat.block_ids||[]).filter(id=>ids.has(id)));
  const groups:BoardGroup[]=[];
  let pending:BoardGroup|null=null;
  let previousKey="";
  const flush=()=>{if(pending)groups.push(pending);pending=null;};
  for(const block of blocks){
    if(block.layout_group||groupedBeats.has(block.id)){
      flush();
      const key=block.layout_group?`layout:${block.layout_group}`:`beat:${root(block.id)}`;
      let group=previousKey===key?groups.at(-1):undefined;
      if(!group){
        group={id:block.layout_group?`${block.layout_group}:${block.id}`:`beat:${block.id}`,block_ids:[]};
        groups.push(group);
      }
      previousKey=key;
      group.block_ids.push(block.id);continue;
    }
    previousKey="";
    // Four legacy definitions followed by a diagram form one knowledge group.
    // A new titled paragraph starts its own topic; images follow their text.
    if(block.type==="text" && block.title && pending)flush();
    if(!pending)pending={id:`content:${block.id}`,block_ids:[]};
    pending.block_ids.push(block.id);
    if(block.type!=="text")flush();
  }
  flush();
  // A streamed stage commonly emits one small text block per beat. Keep the
  // beat-level reveal metadata, but lay adjacent text beats out as one compact
  // knowledge group so each short segment does not consume a mostly empty page.
  const compacted: BoardGroup[] = [];
  for (const group of groups) {
    const beatGroup = group.id.startsWith("beat:");
    const previous = compacted.at(-1);
    if (beatGroup && previous?.id.startsWith("beat:") && previous.block_ids.length + group.block_ids.length <= 5) {
      previous.block_ids.push(...group.block_ids);
    } else {
      compacted.push({...group,block_ids:[...group.block_ids]});
    }
  }
  return compacted;
}
