import {useEffect,useLayoutEffect,useRef,useState} from "react";
import {ArrowLeft,ArrowRight,Download,FileText,Focus,Image as ImageIcon,Minus,Plus,RotateCcw} from "lucide-react";
import {classroomApi} from "../api";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

export type PdfFocus={material_id:string;sha256:string;target_id:string;page_no:number;page_count:number;
  text:string;bbox:[number,number,number,number]|null;precision:string;
  crop_sha256?:string;block_no?:number;crop_width?:number;crop_height?:number};

type LessonProps={conversation:string;focus:PdfFocus;active:boolean;
  onTarget:(value:{x:number;y:number;visible:boolean}|null)=>void;onPainted:()=>void};

export default function PdfLesson(props:LessonProps){
  const [originalTarget,setOriginalTarget]=useState("");
  const target=`${props.focus.material_id}:${props.focus.target_id}:${props.focus.crop_sha256||""}`;
  const original=originalTarget===target;
  return props.focus.crop_sha256&&!original
    ? <SourceCrop key={target} {...props} onOriginal={()=>setOriginalTarget(target)}/>
    : <PdfOriginal {...props} onCrop={props.focus.crop_sha256?()=>setOriginalTarget(""):undefined}/>;
}

function SourceCrop({conversation,focus,active,onTarget,onPainted,onOriginal}:LessonProps&{onOriginal:()=>void}){
  const [zoom,setZoom]=useState(1),[loaded,setLoaded]=useState(false),[error,setError]=useState(false);
  const [retry,setRetry]=useState(0);
  const viewport=useRef<HTMLDivElement>(null),image=useRef<HTMLImageElement>(null);
  const originalUrl=classroomApi.original(conversation,focus.material_id,focus.sha256);
  const url=`${originalUrl.replace(/\/original$/,"")}/figures/${focus.page_no}/${focus.block_no}/${focus.crop_sha256}`;
  useEffect(()=>{
    if(!active||!loaded){onTarget(null);return;}
    const node=viewport.current;if(!node)return;
    const update=()=>{
      const rect=image.current?.getBoundingClientRect(),view=node.getBoundingClientRect();
      if(!rect){onTarget(null);return;}
      onTarget({x:Math.min(rect.right,view.right-12),y:(Math.max(rect.top,view.top)+Math.min(rect.bottom,view.bottom))/2,
        visible:rect.bottom>view.top&&rect.top<view.bottom&&rect.right>view.left&&rect.left<view.right});
    };
    update();node.addEventListener("scroll",update,{passive:true});window.addEventListener("resize",update);
    return()=>{node.removeEventListener("scroll",update);window.removeEventListener("resize",update);onTarget(null);};
  },[active,loaded,zoom,onTarget]);
  return <div className="pdf-lesson" data-pdf-target={loaded?focus.target_id:""} data-source-crop="true">
    <div className="pdf-tools">
      <span>第 {focus.page_no} 页</span>
      <button aria-label="缩小题图" title="缩小" disabled={zoom<=.5} onClick={()=>setZoom(v=>Math.max(.5,v-.25))}><Minus size={17}/></button>
      <button aria-label="放大题图" title="放大" disabled={zoom>=3} onClick={()=>setZoom(v=>Math.min(3,v+.25))}><Plus size={17}/></button>
      <button aria-label="重置题图缩放" title="重置缩放" onClick={()=>setZoom(1)}><Focus size={17}/></button>
      <button aria-label="查看原 PDF 页面" title="查看原页" onClick={onOriginal}><FileText size={17}/></button>
      <a href={`${originalUrl}/download`} download="source.pdf" title="下载原 PDF" aria-label="下载原 PDF"><Download size={17}/></a>
    </div>
    {error&&<div role="alert">题图加载失败<button aria-label="重试题图" title="重试" onClick={()=>{setError(false);setRetry(v=>v+1);}}><RotateCcw size={17}/></button></div>}
    <div className="pdf-viewport" ref={viewport} tabIndex={0} aria-label="资料题图">
      <img key={`${url}:${retry}`} ref={image} src={url} alt={focus.text||"资料原图"}
        width={focus.crop_width} height={focus.crop_height}
        style={{display:"block",width:`${zoom*100}%`,maxWidth:"none",height:"auto"}}
        onLoad={()=>{setLoaded(true);setError(false);onPainted();}} onError={()=>{setLoaded(false);setError(true);}}/>
    </div>
  </div>;
}

function PdfOriginal({conversation,focus,active,onTarget,onPainted,onCrop}:LessonProps&{onCrop?:()=>void}){
  const [document,setDocument]=useState<any>(null),[page,setPage]=useState(focus.page_no),[zoom,setZoom]=useState(1);
  const [following,setFollowing]=useState(true),[error,setError]=useState(""),[retry,setRetry]=useState(0);
  const [size,setSize]=useState({width:640,height:900}),[width,setWidth]=useState(640),[painted,setPainted]=useState("");
  const canvas=useRef<HTMLCanvasElement>(null),viewport=useRef<HTMLDivElement>(null),box=useRef<HTMLDivElement>(null);
  const userScroll=useRef(false),programmatic=useRef(false);
  const key=`${focus.material_id}:${focus.sha256}`;
  const url=classroomApi.original(conversation,focus.material_id,focus.sha256);
  useEffect(()=>{
    let task:any,cancelled=false;setDocument(null);setError("");setPage(focus.page_no);setFollowing(true);
    void import("pdfjs-dist").then(pdf=>{
      if(cancelled)return;
      pdf.GlobalWorkerOptions.workerSrc=workerUrl;
      task=pdf.getDocument({url,withCredentials:true,enableXfa:false});
      return task.promise.then((doc:any)=>{if(!cancelled)setDocument(doc);});
    }).catch(()=>{if(!cancelled)setError("原 PDF 加载失败，可能已删除或版本失效。");});
    return()=>{cancelled=true;void task?.destroy();};
  },[key,conversation,retry]);
  useLayoutEffect(()=>{
    const node=viewport.current;if(!node)return;
    const update=()=>setWidth(Math.max(220,node.clientWidth-24));
    update();const observer=new ResizeObserver(update);observer.observe(node);return()=>observer.disconnect();
  },[active]);
  useEffect(()=>{if(following)setPage(focus.page_no);},[focus.target_id,focus.material_id,following]);
  useEffect(()=>{
    if(!document||!canvas.current||!active)return;
    let cancelled=false,render:any;
    setPainted("");
    document.getPage(page).then((sheet:any)=>{
      if(cancelled)return;
      const base=sheet.getViewport({scale:1}),view=sheet.getViewport({scale:width/base.width*zoom});
      const ratio=Math.min(2,window.devicePixelRatio||1),node=canvas.current!;
      node.width=Math.round(view.width*ratio);node.height=Math.round(view.height*ratio);
      setSize({width:view.width,height:view.height});
      render=sheet.render({canvasContext:node.getContext("2d"),canvas:node,viewport:view,
        transform:[ratio,0,0,ratio,0,0]});
      return render.promise.then(()=>{if(!cancelled){setPainted(`${key}:${page}`);onPainted();}});
    }).catch((error:any)=>{if(!cancelled&&error?.name!=="RenderingCancelledException")setError("此页渲染失败，可重试。");});
    return()=>{cancelled=true;render?.cancel();};
  },[document,page,width,zoom,active,key,onPainted]);
  const pointed=page===focus.page_no&&painted===`${key}:${page}`;
  useLayoutEffect(()=>{
    const node=viewport.current;
    if(!node||!following||!pointed||!active)return;
    programmatic.current=true;
    userScroll.current=false;
    const y=focus.bbox?focus.bbox[1]*size.height:0,x=focus.bbox?focus.bbox[0]*size.width:0;
    node.scrollTop=Math.max(0,y-node.clientHeight*.3);node.scrollLeft=Math.max(0,x-24);
    const frame=requestAnimationFrame(()=>{programmatic.current=false;});
    return()=>cancelAnimationFrame(frame);
  },[pointed,focus.target_id,following,size,active]);
  useEffect(()=>{
    if(!active||!pointed||!following||!focus.bbox){onTarget(null);return;}
    const node=viewport.current;if(!node)return;
    const update=()=>{
      const rect=box.current?.getBoundingClientRect(),view=node.getBoundingClientRect();
      if(!rect){onTarget(null);return;}
      const x=Math.min(rect.right,view.right-12),y=(Math.max(rect.top,view.top)+Math.min(rect.bottom,view.bottom))/2;
      onTarget({x,y,visible:rect.bottom>view.top&&rect.top<view.bottom&&rect.right>view.left&&rect.left<view.right});
    };
    update();node.addEventListener("scroll",update,{passive:true});
    window.addEventListener("resize",update);
    return()=>{node.removeEventListener("scroll",update);window.removeEventListener("resize",update);};
  },[active,pointed,following,focus.target_id,focus.bbox,size,onTarget]);
  return <div className="pdf-lesson" data-pdf-target={pointed?focus.target_id:""} data-pdf-following={following}>
    <div className="pdf-tools">
      {onCrop&&<button aria-label="查看题图裁片" title="查看题图" onClick={onCrop}><ImageIcon size={17}/></button>}
      <button aria-label="上一页原文" title="上一页" disabled={page<=1} onClick={()=>{setFollowing(false);setPage(p=>p-1);}}><ArrowLeft size={17}/></button>
      <span>{page} / {document?.numPages||focus.page_count}</span>
      <button aria-label="下一页原文" title="下一页" disabled={page>=(document?.numPages||focus.page_count)} onClick={()=>{setFollowing(false);setPage(p=>p+1);}}><ArrowRight size={17}/></button>
      <button aria-label="缩小原文" title="缩小" disabled={zoom<=.5} onClick={()=>setZoom(value=>Math.max(.5,value-.25))}><Minus size={17}/></button>
      <button aria-label="放大原文" title="放大" disabled={zoom>=3} onClick={()=>setZoom(value=>Math.min(3,value+.25))}><Plus size={17}/></button>
      <button aria-label="回到原文讲解位置" title="回到讲解位置" onClick={()=>{setFollowing(true);setPage(focus.page_no);}}><Focus size={17}/></button>
      <a href={`${url}/download`} download="source.pdf" title="下载原 PDF" aria-label="下载原 PDF"><Download size={17}/></a>
    </div>
    {error?<div role="alert">{error}<button title="重试原文" aria-label="重试原文" onClick={()=>setRetry(value=>value+1)}><RotateCcw size={17}/></button></div>:null}
    {focus.precision==="page"&&<div className="pdf-precision">当前仅能定位到原页。</div>}
    <div className="pdf-viewport" ref={viewport} tabIndex={0} aria-label="PDF 原文"
      onWheel={()=>{userScroll.current=true;setFollowing(false);}} onTouchStart={()=>{userScroll.current=true;setFollowing(false);}}
      onPointerDown={()=>{userScroll.current=true;}}
      onScroll={()=>{if(userScroll.current&&!programmatic.current){setFollowing(false);userScroll.current=false;}}}
      onKeyDown={event=>{if(["ArrowUp","ArrowDown","PageUp","PageDown","Home","End"," "].includes(event.key))setFollowing(false);}}>
      <div className="pdf-page" style={size}>
        <canvas ref={canvas} aria-label={`原文第 ${page} 页`} style={size}/>
        {pointed&&focus.bbox&&<div className="pdf-focus-box" ref={box} aria-label="当前讲解原文区域"
          style={{left:focus.bbox[0]*size.width,top:focus.bbox[1]*size.height,
            width:(focus.bbox[2]-focus.bbox[0])*size.width,height:(focus.bbox[3]-focus.bbox[1])*size.height}}/>}
      </div>
    </div>
  </div>;
}
