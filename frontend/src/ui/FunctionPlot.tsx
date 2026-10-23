import {useEffect,useId,useRef,useState} from "react";
import JXG from "jsxgraph";
import {expression} from "../lib/graphics";
import type {Plot,PlotAnswer,Point} from "../lib/graphics";
import "../../node_modules/jsxgraph/distrib/jsxgraph.css";

export default function FunctionPlot({graphic,ink=false,answer,mode,onChange,locked=false}:
  {graphic:Plot;ink?:boolean;answer?:PlotAnswer;mode?:"point"|"sketch";onChange?:(value:PlotAnswer)=>void;locked?:boolean}){
  const host=useRef<HTMLDivElement>(null),id="plot"+useId().replaceAll(":",""),[error,setError]=useState("");
  const [dark,setDark]=useState(false);
  useEffect(()=>{
    const theme=host.current?.closest("[data-theme]");
    if(!theme)return;
    const sync=()=>setDark(theme.getAttribute("data-theme")==="dark");
    const observer=new MutationObserver(sync);observer.observe(theme,{attributes:true,attributeFilter:["data-theme"]});sync();
    return()=>observer.disconnect();
  },[]);
  const live=useRef({answer,onChange,mode,locked});live.current={answer,onChange,mode,locked};
  const signature=JSON.stringify({graphic,answer,ink,locked});
  useEffect(()=>{
    if(!host.current)return;
    let board:any;
    try{
      const lightInk=ink||dark;
      const {x,y}=graphic.bounds,fg=lightInk?"#e5eee0":"#334637",grid=lightInk?"#526659":"#e1e7e2",highlight=lightInk?"#f2d38c":"#b34837";
      board=JXG.JSXGraph.initBoard(id,{boundingbox:[x[0],y[1],x[1],y[0]],axis:false,showCopyright:false,showNavigation:false,
        keepaspectratio:false,pan:{enabled:!answer},zoom:{wheel:!answer,pinchHorizontal:!answer,pinchVertical:!answer},resize:{enabled:false,throttle:100},renderer:"svg"});
      const step=(span:number)=>{const raw=span/6,unit=10**Math.floor(Math.log10(raw));return Math.ceil(raw/unit)*unit;};
      board.create("grid",[],{strokeColor:grid,strokeOpacity:.55,majorStep:[step(x[1]-x[0]),step(y[1]-y[0])],minorElements:0});
      for(const direction of [[[0,0],[1,0]],[[0,0],[0,1]]]){
        board.create("axis",direction,{strokeColor:fg,strokeWidth:1,ticks:{strokeColor:fg,label:{strokeColor:fg,fontSize:12},minorTicks:0}});
      }
      const data=answer||graphic;
      for(const [index,curve]of data.curves.entries()){
        const fn=expression(curve.expression);
        // Split at domain failures and large adjacent jumps before handing paths to JSXGraph.
        let xs:number[]=[],ys:number[]=[];
        const flush=()=>{if(xs.length>1)board.create("curve",[xs,ys],{strokeColor:lightInk?["#e5eee0","#f2d38c","#b9d8ed","#f3b4ac"][index%4]:["#2959b6","#b34837","#327954","#8251a0"][index%4],strokeWidth:2.5,fixed:true,highlight:false});xs=[];ys=[];};
        for(let i=0;i<=800;i++){
          const v=x[0]+(x[1]-x[0])*i/800,z=fn({x:v});
          if(!Number.isFinite(z)||z<y[0]-(y[1]-y[0])*2||z>y[1]+(y[1]-y[0])*2){flush();continue;}
          if(ys.length&&Math.abs(z-ys[ys.length-1])>(y[1]-y[0])*.65)flush();
          xs.push(v);ys.push(z);
        }flush();
      }
      for(const stroke of answer?.strokes||[])board.create("curve",[stroke.map(p=>p.x),stroke.map(p=>p.y)],{strokeColor:lightInk?"#e5eee0":"#2959b6",strokeWidth:2.5,fixed:true,highlight:false});
      data.points.forEach((point,index)=>{
        const p=board.create("point",[point.x,point.y],{name:point.label||`(${point.x}, ${point.y})`,fixed:!answer||locked,
          size:3,strokeColor:highlight,fillColor:highlight,label:{strokeColor:highlight,fontSize:13,display:"internal",parse:false},showInfobox:false});
        if(answer&&!locked)p.on("up",()=>{
          const current=live.current;if(!current.answer||current.locked)return;
          const points=current.answer.points.map((v,i)=>i===index?{...v,x:Math.round(p.X()*100)/100,y:Math.round(p.Y()*100)/100}:v);
          current.onChange?.({...current.answer,points});
        });
      });
      let stroke:Point[]|null=null,preview:any=null,started=false;
      const coordinate=(event:any)=>{
        const pointer=board.getUsrCoordsOfMouse(event),px=Math.max(x[0],Math.min(x[1],pointer[0])),py=Math.max(y[0],Math.min(y[1],pointer[1]));
        return{x:Math.round(px*1000)/1000,y:Math.round(py*1000)/1000};
      };
      board.on("down",(event:any)=>{
        const current=live.current;if(!current.answer||current.locked)return;
        if(board.getAllUnderMouse(event).some((item:any)=>item.elType==="point"))return;
        started=true;
        if(current.mode==="sketch"&&current.answer.strokes.length<4){
          stroke=[coordinate(event)];
          preview=board.create("curve",[[stroke[0].x],[stroke[0].y]],{strokeColor:lightInk?"#e5eee0":"#2959b6",strokeWidth:2.5,fixed:true});
        }
      });
      board.on("move",(event:any)=>{if(stroke&&stroke.length<300){stroke.push(coordinate(event));preview.dataX=stroke.map(p=>p.x);preview.dataY=stroke.map(p=>p.y);board.update();}});
      board.on("up",(event:any)=>{
        const current=live.current;if(!started||!current.answer||current.locked)return;started=false;
        if(stroke){if(stroke.length>=2)current.onChange?.({...current.answer,strokes:[...current.answer.strokes,stroke]});stroke=null;}
        else if(current.mode==="point"&&current.answer.points.length<12){
          const p=coordinate(event);
          if(!current.answer.points.some(v=>Math.hypot((p.x-v.x)/(x[1]-x[0]),(p.y-v.y)/(y[1]-y[0]))<.025))
            current.onChange?.({...current.answer,points:[...current.answer.points,p]});
        }
      });
      setError("");
      const resize=new ResizeObserver(()=>{if(host.current)board.resizeContainer(host.current.clientWidth,host.current.clientHeight,true);});
      resize.observe(host.current);
      return()=>{resize.disconnect();JXG.JSXGraph.freeBoard(board);};
    }catch{
      setError("图形表达式无法绘制，请检查表达式与坐标范围。");
      if(board)JXG.JSXGraph.freeBoard(board);
    }
  // JSXGraph owns its DOM; regenerate only after semantic data changes.
  },[id,signature,dark]);
  return <div className="function-plot-wrap"><div ref={host} id={id} className="function-plot-host" role="img" aria-label="函数坐标图" style={{visibility:error?"hidden":"visible"}}/>
    {error&&<p role="alert" className="figure-error">{error}</p>}</div>;
}
