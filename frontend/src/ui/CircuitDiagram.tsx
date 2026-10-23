import {useRef,useState} from "react";
import {Trash2,Scan} from "lucide-react";
import {circuitGeometry} from "../lib/graphics";
import type {Circuit} from "../lib/graphics";

export default function CircuitDiagram({value,onChange,locked=false}:{value:Circuit;onChange?:(v:Circuit)=>void;locked?:boolean}){
  const svg=useRef<SVGSVGElement>(null),[selected,setSelected]=useState<string|null>(null),[selectedWire,setWire]=useState<number|null>(null),[pin,setPin]=useState<{id:string;port:number;output:boolean}|null>(null);
  const drag=useRef<{id:string;dx:number;dy:number;moved:boolean}|null>(null);
  const [fit,setFit]=useState(false);
  const live=useRef(value);live.current=value;
  const editable=Boolean(onChange&&!locked),{width,height,pin:position}=circuitGeometry(value);
  const routes=value.wires.map(w=>{
    const a=position(w.source,0,true),b=position(w.target,w.port),mid=(a[0]+b[0])/2;
    return{wire:w,a,b,mid,horizontals:[{x1:a[0],x2:mid,y:a[1]},{x1:mid,x2:b[0],y:b[1]}],vertical:{x:mid,y1:Math.min(a[1],b[1]),y2:Math.max(a[1],b[1])}};
  });
  const bridges:{x:number;y:number}[]=[];
  for(let i=0;i<routes.length;i++)for(let j=i+1;j<routes.length;j++){
    for(const h of routes[i].horizontals)for(const v of [routes[j].vertical]){
      if(v.x>Math.min(h.x1,h.x2)+5&&v.x<Math.max(h.x1,h.x2)-5&&h.y>v.y1+5&&h.y<v.y2-5)bridges.push({x:v.x,y:h.y});
    }
    for(const h of routes[j].horizontals)for(const v of [routes[i].vertical]){
      if(v.x>Math.min(h.x1,h.x2)+5&&v.x<Math.max(h.x1,h.x2)-5&&h.y>v.y1+5&&h.y<v.y2-5)bridges.push({x:v.x,y:h.y});
    }
  }
  function connect(id:string,port:number,output:boolean){
    if(!editable)return;
    if(!pin){setPin({id,port,output});return;}
    if(pin.output!==output&&pin.id!==id){
      const from=pin.output?pin:{id,port,output},to=pin.output?{id,port,output}:pin;
      const wires=value.wires.filter(w=>!(w.target===to.id&&w.port===to.port));
      if(wires.length<96)onChange?.({...value,wires:[...wires,{source:from.id,target:to.id,port:to.port}]});
    }
    setPin(null);
  }
  const local=(e:React.PointerEvent)=>new DOMPoint(e.clientX,e.clientY).matrixTransform(svg.current!.getScreenCTM()!.inverse());
  const terminal=(id:string,port:number,output:boolean)=>{
    const [x,y]=position(id,port,output),label=output?`${id} 输出`:`${id} 输入 ${port+1}`,chosen=pin?.id===id&&pin.port===port&&pin.output===output;
    return <g key={`${id}:${port}:${output}`} role={editable?"button":undefined} tabIndex={editable?0:undefined} aria-label={label} aria-pressed={editable?Boolean(chosen):undefined}
      className={`circuit-pin ${chosen?"pending":""}`} onClick={e=>{e.stopPropagation();connect(id,port,output);}}
      onKeyDown={e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();connect(id,port,output);}}}>
      <title>{label}</title><circle cx={x} cy={y} r="12" fill="transparent" stroke="none"/>
      <rect x={x-3} y={y-3} width="6" height="6" className="pin-mark"/>
    </g>;
  };
  return <div className="circuit-container"><div className="circuit-viewport"><svg ref={svg} className="circuit-diagram" style={fit?{minWidth:0}:undefined} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={editable?"电路作图区":"组合逻辑电路"}
    onPointerMove={e=>{
      const state=drag.current;if(!state||!editable)return;const p=local(e);
      const g=svg.current?.querySelector<SVGGElement>(`[data-gate="${state.id}"]`);
      const x=Math.max(100,Math.min(1200,width-160,p.x-state.dx)),y=Math.max(8,Math.min(800,height-100,p.y-state.dy));
      if(g){g.setAttribute("transform",`translate(${x},${y})`);g.dataset.dragX=String(x);g.dataset.dragY=String(y);}
      state.moved=true;
    }}
    onPointerUp={()=>{
      const state=drag.current;drag.current=null;if(!state||!state.moved||!editable)return;
      const g=svg.current?.querySelector<SVGGElement>(`[data-gate="${state.id}"]`);
      if(g)onChange?.({...live.current,gates:live.current.gates.map(n=>n.id===state.id?{...n,x:Math.round(Number(g.dataset.dragX)),y:Math.round(Number(g.dataset.dragY))}:n)});
    }} onPointerCancel={()=>{drag.current=null;}}>
    {routes.map(({wire:w,a,b,mid},i)=>{const d=`M${a}H${mid}V${b[1]}H${b[0]}`;
      return <g key={i}><path className={`circuit-wire ${selectedWire===i?"selected":""}`} d={d}/>
        {editable&&<path d={d} fill="none" stroke="transparent" strokeWidth="14" role="button" tabIndex={0} aria-label={`连线 ${w.source} 到 ${w.target} 输入 ${w.port+1}`}
          onClick={()=>{setWire(i);setSelected(null);}}
          onKeyDown={e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();setWire(i);setSelected(null);}
            if(e.key==="Delete"||e.key==="Backspace"){e.preventDefault();onChange?.({...value,wires:value.wires.filter((_,j)=>j!==i)});}}}/>}
      </g>;})}
    {bridges.map((cross,i)=>{
      const d=`M${cross.x-6},${cross.y} Q${cross.x},${cross.y-7} ${cross.x+6},${cross.y}`;
      return <g key={`bridge-${i}`} aria-hidden="true"><path className="circuit-bridge-cut" d={d}/><path className="circuit-bridge" d={d}/></g>;
    })}
    {[...new Set(value.wires.filter(w=>value.wires.filter(other=>other.source===w.source).length>1).map(w=>w.source))]
      .map(id=>{const[x,y]=position(id,0,true);return <circle key={`junction-${id}`} cx={x} cy={y} r="3.2" className="circuit-junction"/>;})}
    {value.inputs.map(id=>{const [x,y]=position(id);return <text key={id} x={x-18} y={y+5} textAnchor="end">{id}</text>;})}
    {value.outputs.map(id=>{const [x,y]=position(id);return <text key={id} x={x+14} y={y+5}>{id}</text>;})}
    {value.gates.map(g=><g key={g.id} data-gate={g.id} transform={`translate(${g.x},${g.y})`} role={editable?"button":undefined}
      tabIndex={editable?0:undefined} aria-label={`${g.type} ${g.id}`} className={`circuit-gate ${selected===g.id?"selected":""}`}
      onPointerDown={e=>{if(!editable)return;setSelected(g.id);setWire(null);const p=local(e);drag.current={id:g.id,dx:p.x-g.x,dy:p.y-g.y,moved:false};svg.current?.setPointerCapture(e.pointerId);}}
      onKeyDown={e=>{
        if(!editable)return;
        if(e.key==="Delete"||e.key==="Backspace"){e.preventDefault();onChange?.({...value,gates:value.gates.filter(n=>n.id!==g.id),wires:value.wires.filter(w=>w.source!==g.id&&w.target!==g.id)});}
        if(["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(e.key)){e.preventDefault();onChange?.({...value,gates:value.gates.map(n=>n.id===g.id?{...n,x:Math.max(100,Math.min(1200,width-160,n.x+(e.key==="ArrowLeft"?-8:e.key==="ArrowRight"?8:0))),y:Math.max(8,Math.min(800,height-100,n.y+(e.key==="ArrowUp"?-8:e.key==="ArrowDown"?8:0)))}:n)});}
      }}>
      {selected===g.id&&editable&&<rect className="circuit-selection" x="-8" y="-7" width="91" height="70"/>}
      {["NOT","BUFFER"].includes(g.type)?<><path className="circuit-symbol" d="M0,4L55,28L0,52Z"/>{g.type==="NOT"&&<circle className="circuit-symbol" cx="61" cy="28" r="5"/>}<path className="circuit-wire" d={`M${g.type==="NOT"?66:55},28H76`}/></>:
        ["AND","NAND"].includes(g.type)?<><path className="circuit-symbol" d="M0,0H35A28,28 0 0 1 35,56H0Z"/>{g.type==="NAND"&&<circle className="circuit-symbol" cx="68" cy="28" r="5"/>}<path className="circuit-wire" d={`M${g.type==="NAND"?73:63},28H76`}/></>:
          <><path className="circuit-symbol" d="M0,0Q39,0 65,28Q39,56 0,56Q19,28 0,0Z"/><path className="circuit-wire" d="M0,16H8 M0,40H8"/>
            {["XOR","XNOR"].includes(g.type)&&<path className="circuit-wire" d="M-6,0Q13,28 -6,56"/>}
            {["NOR","XNOR"].includes(g.type)&&<circle className="circuit-symbol" cx="70" cy="28" r="5"/>}<path className="circuit-wire" d={`M${["NOR","XNOR"].includes(g.type)?75:65},28H76`}/></>}
      <text x="34" y="77" textAnchor="middle" className="gate-label">{g.type}</text>
    </g>)}
    {value.inputs.map(id=>terminal(id,0,true))}
    {value.outputs.map(id=>terminal(id,0,false))}
    {value.gates.flatMap(g=>[terminal(g.id,0,true),terminal(g.id,0,false),...(["NOT","BUFFER"].includes(g.type)?[]:[terminal(g.id,1,false)])])}
  </svg></div>
    <button type="button" title="适应电路画面" aria-label="适应电路画面" aria-pressed={fit} className="circuit-fit" onClick={()=>setFit(v=>!v)}><Scan size={16}/></button>
    {editable&&(selected||selectedWire!==null)&&<button type="button" title="删除所选对象" aria-label="删除所选对象" className="circuit-remove" onClick={()=>{
      if(selected)onChange?.({...value,gates:value.gates.filter(g=>g.id!==selected),wires:value.wires.filter(w=>w.source!==selected&&w.target!==selected)});
      else onChange?.({...value,wires:value.wires.filter((_,j)=>j!==selectedWire)});
      setSelected(null);setWire(null);
    }}><Trash2 size={16}/></button>}
    {editable&&pin&&<span role="status" className="circuit-pending">已选择 {pin.id} 的{pin.output?"输出":"输入"}引脚</span>}
  </div>;
}
