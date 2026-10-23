import {useEffect,useState} from "react";
import {Plus,Save,Trash2,Download} from "lucide-react";
import {api} from "../api";
import {errText} from "../lib/errors";

export default function PersonaEditor({card,editable,prepare,onSaved}:{card:any;editable:boolean;
  prepare:()=>Promise<any>;onSaved:(card:any)=>void}){
  const [skill,setSkill]=useState(""),[skills,setSkills]=useState<any[]>([]);
  const [busy,setBusy]=useState(false),[message,setMessage]=useState("");
  useEffect(()=>{setSkill(card?.persona?.skill||"");setSkills(card?.persona?.skills||[]);},
    [card?.card_id,card?.revision]);
  useEffect(()=>{setMessage("");},[card?.card_id]);
  const change=(index:number,field:string,value:unknown)=>setSkills(rows=>rows.map((row,i)=>i===index?{...row,[field]:value}:row));
  return <section className="character-section persona-editor">
    <h2>人物思维与 Skill</h2>
    {card?.persona?.quality?.grade&&<p role="status">{({
      limited:"资料有限 · 人物可用",grounded:"资料较充分 · 人物可用",authored:"原创设定验收通过",draft:"存在硬性待修订项"
    } as Record<string,string>)[card.persona.quality.grade]||"待核对"}
      {card.persona.quality.default_eligible?" · 课堂导师验收通过":""}</p>}
    {!!card?.persona?.quality?.warnings?.length&&<details><summary>资料与角色边界</summary>
      <ul>{card.persona.quality.warnings.map((warning:string,index:number)=><li key={index}>{warning}</li>)}</ul></details>}
    <label>完整人物 Skill<textarea aria-label="完整人物 Skill" rows={10} value={skill} maxLength={64000} disabled={!editable||busy}
      onChange={event=>setSkill(event.target.value)}/></label>
    <div className="row">
      {editable&&<button className="btn" disabled={busy} onClick={async()=>{
        const persona={...(card?.persona||{}),skill,skills,source_kind:card?.persona?.source_kind||"custom"};
        setBusy(true);setMessage("");
        try{const saved=await prepare();const updated=await api.savePersona(saved.card_id,saved.revision,persona);
          onSaved(updated);setMessage("完整人物 Skill 已保存");}
        catch(error){setMessage(errText(error));}finally{setBusy(false);}
      }}><Save size={16}/>保存人物 Skill</button>}
      {card?.persona&&<button className="iconbtn" title="导出人物产物" aria-label="导出人物产物" onClick={()=>{
        const url=URL.createObjectURL(new Blob([JSON.stringify(card.persona,null,2)],{type:"application/json"}));
        const link=document.createElement("a");link.href=url;link.download="persona.json";link.click();URL.revokeObjectURL(url);
      }}><Download size={18}/></button>}
    </div>
    {message&&<p role="status">{message}</p>}
    <h3>挂载能力</h3>
    {skills.map((entry,index)=><div className="stack persona-mounted" key={index}>
      <div className="row">
        <label>名称<input value={entry.name} maxLength={80} disabled={!editable} onChange={e=>change(index,"name",e.target.value)}/></label>
        <label>版本<input value={entry.version} maxLength={40} disabled={!editable} onChange={e=>change(index,"version",e.target.value)}/></label>
        <label><input type="checkbox" checked={entry.enabled} disabled={!editable} onChange={e=>change(index,"enabled",e.target.checked)}/>启用</label>
        {editable&&<button className="iconbtn" title="移除能力" aria-label="移除能力" onClick={()=>setSkills(rows=>rows.filter((_,i)=>i!==index))}><Trash2 size={16}/></button>}
      </div>
      <label>能力指令<textarea rows={5} value={entry.instructions} maxLength={20000} disabled={!editable}
        onChange={e=>change(index,"instructions",e.target.value)}/></label>
    </div>)}
    {editable&&<button className="btn" disabled={skills.length>=8} onClick={()=>setSkills(rows=>[...rows,{name:"",version:"1",instructions:"",enabled:true}])}><Plus size={16}/>添加能力 Skill</button>}
    {card?.persona?.references?.length>0&&<><h3>研究原稿</h3>{card.persona.references.map((reference:any)=>
      <details key={reference.name}><summary>{reference.name}</summary><pre>{reference.body}</pre></details>)}</>}
    {card?.persona?.quality&&Object.keys(card.persona.quality).length>0&&<details><summary>验证与精炼报告</summary>
      <pre>{JSON.stringify(card.persona.quality,null,2)}</pre></details>}
    {card?.persona?.provenance&&Object.keys(card.persona.provenance).length>0&&<details><summary>来源与提炼依据</summary>
      <pre>{JSON.stringify(card.persona.provenance,null,2)}</pre></details>}
  </section>;
}
