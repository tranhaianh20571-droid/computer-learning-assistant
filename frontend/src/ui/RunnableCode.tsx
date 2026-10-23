import {Play,Square,RotateCcw,Pencil} from "lucide-react";
import {useEffect,useRef} from "react";
import type {ClassroomCode} from "../hooks/useInteractiveClassroom";
import {cellKey} from "../hooks/useInteractiveClassroom";

export default function RunnableCode({block,context,onEdit,explaining=false}:{block:any;context:ClassroomCode;onEdit?:()=>void;explaining?:boolean}){
  const step=block.source_step??1,cell=context.get(step,block),key=cellKey(step,block.id,block.source_generation||1);
  const running=context.running===key;
  const output=useRef<HTMLDivElement>(null);
  const editor=useRef<HTMLTextAreaElement>(null);
  useEffect(()=>{
    if(running||!cell.runs||cell.status==="idle")return;
    const node=output.current;
    if(editor.current){editor.current.scrollTop=0;editor.current.scrollLeft=0;}
    node?.scrollIntoView({block:"nearest",inline:"nearest"});
    if(node)node.scrollTop=cell.status==="error"?node.scrollHeight:0;
  },[running,cell.runs,cell.status]);
  return <div className="runnable-code" data-executable-code={block.id}>
    <div className="code-cell-tools">
      <span>{block.language}</span>
      <button type="button" title={running?"停止运行":"运行代码"} aria-label={running?"停止运行":"运行代码"}
        disabled={!context.ready||Boolean(context.running&&!running)}
        onClick={()=>running?context.stop():void context.run(step,block)}>{running?<Square size={16}/>:<Play size={16}/>}</button>
      <button type="button" title="恢复示例" aria-label="恢复示例" disabled={Boolean(context.running)}
        onClick={()=>{onEdit?.();context.update({...cell,code:cell.original_code,output:"",images:[],status:"idle"});}}><RotateCcw size={16}/></button>
      {explaining&&<button type="button" title="编辑代码" aria-label="编辑代码" onClick={onEdit}><Pencil size={16}/></button>}
    </div>
    {explaining?<pre className="code-cell-narrated">{cell.code.split("\n").map((line,index)=>
      <span key={index} data-code-line={index+1} className={cell.code===cell.original_code&&block.active_lines?.includes(index+1)?"teaching-active-code-line":""}>{line||" "}</span>)}</pre>:
    <textarea ref={editor} aria-label={`${block.title||"代码"}，可编辑`} spellCheck={false} value={cell.code} maxLength={12000}
      disabled={!context.ready||Boolean(context.running)} onFocus={onEdit} onBlur={()=>void context.save()}
      onChange={event=>context.update({...cell,code:event.target.value,status:"idle",output:"",images:[]})}
      onKeyDown={event=>{
        event.stopPropagation();
        if(event.key==="Tab"){event.preventDefault();const node=event.currentTarget,start=node.selectionStart,end=node.selectionEnd;
          context.update({...cell,code:cell.code.slice(0,start)+"    "+cell.code.slice(end),status:"idle",output:"",images:[]});
          requestAnimationFrame(()=>{node.selectionStart=node.selectionEnd=start+4;});}
      }}/>}
    <div ref={output} className="code-cell-output" role="status" aria-label="代码运行结果" data-status={running?"running":cell.status}>
      {running?<span>正在运行…</span>:cell.output?<pre>{cell.output}</pre>:cell.status==="success"?<span>运行完成，无文本输出。</span>:null}
      {cell.images?.map((image,index)=><img key={index} src={image} alt={`运行结果图 ${index+1}`}/>)}
    </div>
  </div>;
}
