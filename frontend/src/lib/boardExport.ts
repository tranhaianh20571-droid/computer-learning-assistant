import {TeachingBuffer} from "./teachingBuffer.ts";

export function boardExportScenes(workspace:any,buffer:any,current:number,scene:any,all:boolean,cells:any[]=[]){
  const scenes:any[]=[];
  if(!all&&scene)scenes.push(scene);
  else{
    const stages=new Map<string,any>();
    for(const step of [...workspace.steps].sort((a,b)=>a.step_no-b.step_no)){
      const cursor=step.step_no===current?buffer.cursor(`step:${current}`).visible_chars:0;
      const chars=Math.max(cursor,workspace.played_steps?.[step.step_no]?.visible_chars||0);
      if(!chars)continue;
      const store=new TeachingBuffer();
      store.receive("export",{text:step.text,board:step.board,beats:step.board?.beats||[],final:true});
      store.restoreCursor("export",chars);
      const shown=store.view("export")?.board;if(!shown?.presentation)continue;
      const key=step.board_scope||`step:${step.step_no}`;
      let stage=stages.get(key);
      if(!stage){stage={...shown,presentation:{...shown.presentation,blocks:[]},original_refs:[]};stages.set(key,stage);}
      const blocks=new Map(stage.presentation.blocks.map((block:any)=>[block.id,block]));
      for(const block of shown.presentation.blocks)blocks.set(block.id,{...block,source_step:step.step_no});
      stage.presentation.blocks=Array.from(blocks.values());
      const refs=new Map(stage.original_refs.map((ref:any)=>[`${ref.material_id}:${ref.figure_id}`,ref]));
      for(const ref of shown.original_refs||[])refs.set(`${ref.material_id}:${ref.figure_id}`,ref);
      stage.original_refs=Array.from(refs.values());
      stage.pdf_focus=shown.pdf_focus;
    }
    scenes.push(...stages.values());
  }
  return structuredClone(scenes).map(scene=>{
    if(!scene.presentation)return scene;
    const blocks=scene.presentation.blocks.map((block:any)=>{
      if(block.type!=="code")return block;
      const cell=cells.find(cell=>cell.step_no===block.source_step&&cell.block_id===block.id&&
        (cell.generation||1)===(block.source_generation||1));
      return cell&&(block.reveal_progress??1)>=1?
        {...block,code:cell.code,export_output:cell.output,export_modified:cell.code!==cell.original_code,
          export_images:(cell.images||[]).filter((image:string)=>/^data:image\/(?:png|jpeg);base64,/.test(image))}:block;
    });
    return {...scene,presentation:{...scene.presentation,blocks,all_blocks:blocks,writing:null}};
  });
}
