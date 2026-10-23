import {useRef,useState} from "react";
import {Plus,Undo2,Redo2,RotateCcw,Trash2,MousePointer2,Pencil,MapPin} from "lucide-react";
import {circuitIssues,emptyDrawing,expression} from "../lib/graphics";
import type {DrawingAnswer,DrawingSpec,GateKind} from "../lib/graphics";
import FunctionPlot from "./FunctionPlot";
import CircuitDiagram from "./CircuitDiagram";
import "./professionalFigure.css";

export default function DrawingWorkspace({spec,value,onChange,locked=false}:
  {spec:DrawingSpec;value?:DrawingAnswer;onChange:(v:DrawingAnswer)=>void;locked?:boolean}){
  const current=value||emptyDrawing(spec),past=useRef<DrawingAnswer[]>([]),future=useRef<DrawingAnswer[]>([]);
  const [mode,setMode]=useState<"point"|"sketch">(spec.kind==="plot"&&spec.allow_sketch?"sketch":"point");
  const [formula,setFormula]=useState(""),[error,setError]=useState(""),[status,setStatus]=useState(""),[point,setPoint]=useState({x:"",y:""});
  const [gateType,setGateType]=useState<GateKind>(spec.kind==="circuit"?spec.allowed_gates[0]:"AND");
  function change(next:DrawingAnswer){
    if(locked)return;
    past.current.push(structuredClone(current));if(past.current.length>40)past.current.shift();
    future.current=[];onChange(next);setError("");setStatus("");
  }
  function history(redo=false){
    if(locked)return;
    const source=redo?future.current:past.current,target=redo?past.current:future.current,next=source.pop();
    if(next){target.push(structuredClone(current));onChange(next);setError("");setStatus("");}
  }
  const plot=current.kind==="plot"?current:null;
  return <div className="drawing-workspace" data-kind={spec.kind}>
    <div className="drawing-tools">
      {spec.kind==="circuit"&&current.kind==="circuit"?<div className="figure-tools">
        <label className="sr-only" htmlFor="gate-kind">逻辑门类型</label>
        <select id="gate-kind" aria-label="逻辑门类型" disabled={locked} value={gateType} onChange={e=>setGateType(e.target.value as GateKind)}>
          {spec.allowed_gates.map(t=><option key={t} value={t}>{{NOT:"非门",AND:"与门",OR:"或门",XOR:"异或门",NAND:"与非门",NOR:"或非门",XNOR:"同或门",BUFFER:"缓冲器"}[t]}</option>)}
        </select>
        <button type="button" aria-label="添加逻辑门" title="添加逻辑门" disabled={locked||current.gates.length>=32} onClick={()=>{
          let id="g"+(current.gates.length+1);while([...current.inputs,...current.outputs,...current.gates.map(g=>g.id)].includes(id))id="g"+crypto.randomUUID().slice(0,8);
          change({...current,gates:[...current.gates,{id,type:gateType,x:180+current.gates.length%3*130,y:Math.min(700,25+Math.floor(current.gates.length/3)*70)}]});
        }}><Plus size={17}/></button>
        <button type="button" disabled={locked} onClick={()=>setStatus(circuitIssues(current).join("；")||"输入引脚连接完整。")}>检查连接</button>
      </div>:<div className="figure-tools" role="group" aria-label="作图模式">
        {spec.kind==="plot"&&spec.allow_sketch&&<button type="button" title="画曲线" aria-label="画曲线" aria-pressed={mode==="sketch"} disabled={locked} onClick={()=>setMode("sketch")}><Pencil size={17}/></button>}
        {spec.kind==="plot"&&spec.allow_points&&<button type="button" title="标点" aria-label="标点" aria-pressed={mode==="point"} disabled={locked} onClick={()=>setMode("point")}><MapPin size={17}/></button>}
        <span className="drawing-mode"><MousePointer2 size={14}/>{mode==="sketch"?"曲线":"标点"}</span>
      </div>}
      <div className="figure-tools">
        <button type="button" title="撤销作图" aria-label="撤销作图" disabled={locked||!past.current.length} onClick={()=>history()}><Undo2 size={17}/></button>
        <button type="button" title="重做作图" aria-label="重做作图" disabled={locked||!future.current.length} onClick={()=>history(true)}><Redo2 size={17}/></button>
        <button type="button" title="恢复起始图" aria-label="恢复起始图" disabled={locked} onClick={()=>change(emptyDrawing(spec))}><RotateCcw size={17}/></button>
      </div>
    </div>
    {spec.kind==="plot"&&plot?<><div className="drawing-plot">
      <FunctionPlot graphic={{kind:"plot",bounds:spec.bounds,curves:[],points:[]}} answer={plot} mode={mode} locked={locked} onChange={change}/>
    </div>
      {spec.allow_expression&&<form className="drawing-expression" onSubmit={e=>{
        e.preventDefault();if(locked)return;
        try{expression(formula);if(plot.curves.length>=4)throw Error("最多保留4条函数曲线");change({...plot,curves:[...plot.curves,{expression:formula}]});setFormula("");}
        catch(e){setError(e instanceof Error?e.message:"表达式无法绘制");}
      }}><label htmlFor="drawing-expression">y =</label><input id="drawing-expression" value={formula} maxLength={240} disabled={locked} onChange={e=>setFormula(e.target.value)} aria-label="函数表达式"/><button type="submit" aria-label="添加函数曲线" title="添加函数曲线" disabled={locked}><Plus size={17}/></button></form>}
      {plot.curves.map((c,i)=><div className="drawing-item" key={i}><span>y = {c.expression}</span><button type="button" title="删除曲线" aria-label={`删除曲线 ${i+1}`} disabled={locked} onClick={()=>change({...plot,curves:plot.curves.filter((_,j)=>j!==i)})}><Trash2 size={16}/></button></div>)}
      {spec.allow_points&&<form className="drawing-expression" onSubmit={e=>{
        e.preventDefault();if(locked)return;
        const x=Number(point.x),y=Number(point.y);
        if(!point.x.trim()||!point.y.trim()||!Number.isFinite(x)||!Number.isFinite(y)||Math.abs(x)>1e4||Math.abs(y)>1e4||plot.points.length>=12){setError("请填写有效坐标，最多12个点。");return;}
        change({...plot,points:[...plot.points,{x,y}]});setPoint({x:"",y:""});
      }}><label htmlFor="point-x">x</label><input id="point-x" aria-label="标点横坐标" type="number" step="any" value={point.x} disabled={locked} onChange={e=>setPoint({...point,x:e.target.value})}/>
        <label htmlFor="point-y">y</label><input id="point-y" aria-label="标点纵坐标" type="number" step="any" value={point.y} disabled={locked} onChange={e=>setPoint({...point,y:e.target.value})}/>
        <button type="submit" title="添加标点" aria-label="添加标点" disabled={locked}><Plus size={17}/></button></form>}
      {plot.points.map((p,i)=><div className="drawing-item" key={i}><span>({p.x}, {p.y})</span><button type="button" title="删除标点" aria-label={`删除标点 ${i+1}`} disabled={locked} onClick={()=>change({...plot,points:plot.points.filter((_,j)=>j!==i)})}><Trash2 size={16}/></button></div>)}
    </>:current.kind==="circuit"?<CircuitDiagram value={current} locked={locked} onChange={change}/>:null}
    <div className="drawing-footer" aria-live="polite">{locked?"作答已锁定":status||"作图草稿"}{error&&<span role="alert" className="drawing-error">{error}</span>}</div>
  </div>;
}
