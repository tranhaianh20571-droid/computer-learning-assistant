/** Local knowledge groups from existing semantic beats, with legacy adjacency fallback. */
export type BoardGroup = { id: string; block_ids: string[] };
export function boardGroups(blocks: any[], beats: {block_ids?:string[]}[] = []): BoardGroup[] {
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
  const flush=()=>{if(pending)groups.push(pending);pending=null;};
  for(const block of blocks){
    if(block.layout_group){
      flush();
      let group=groups.find(item=>item.id===block.layout_group);
      if(!group){group={id:block.layout_group,block_ids:[]};groups.push(group);}
      group.block_ids.push(block.id);continue;
    }
    if(groupedBeats.has(block.id)){
      flush();const id=`beat:${root(block.id)}`;
      let group=groups.find(item=>item.id===id);
      if(!group){group={id,block_ids:[]};groups.push(group);}
      group.block_ids.push(block.id);continue;
    }
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
    if (beatGroup && previous?.id.startsWith("beats:") && previous.block_ids.length + group.block_ids.length <= 5) {
      previous.block_ids.push(...group.block_ids);
    } else if (beatGroup && previous?.id.startsWith("beat:") && previous.block_ids.length + group.block_ids.length <= 5) {
      previous.id = `beats:${previous.id.slice(5)}+${group.id.slice(5)}`;
      previous.block_ids.push(...group.block_ids);
    } else {
      compacted.push({...group,block_ids:[...group.block_ids]});
    }
  }
  return compacted;
}
