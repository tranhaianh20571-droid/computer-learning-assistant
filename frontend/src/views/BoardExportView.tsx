import {useEffect,useRef,useState} from "react";
import {layoutBoard} from "../lib/boardLayout";
import {presentBoard} from "../lib/boardPresentation";
import {boardGroups} from "../lib/boardGroups";
import {setBoardFontFamily} from "../lib/boardText";
import {measureFormula,renderFormulaBlock} from "../lib/formula";
import ProfessionalFigure from "../ui/ProfessionalFigure";
import {api} from "../api";
import "../lib/classroomMedia";
import "katex/dist/katex.min.css";
import "./boardExport.css";

function ExportPage({scene,page,svg}:{scene:any;page:any;svg:string}){
  const blocks=new Map<string,any>(scene.presentation.blocks.map((b:any)=>[b.id,b]));
  return <section className="export-sheet" style={{width:1100,height:page.height}} data-export-page>
    <div className="export-svg" dangerouslySetInnerHTML={{__html:svg}}/>
    {page.placements.map((item:any)=>{
      const block=blocks.get(item.id),body=item.formulaBody||item.codeBody||item.figureBody;
      if(!block||!body)return null;
      const formula=item.formulaBody?renderFormulaBlock(block):null;
      return <div key={item.id} className="export-overlay" style={{left:body.x,top:body.y,width:body.width,height:body.height}}>
        {item.formulaBody?(formula?.valid?<div dangerouslySetInnerHTML={{__html:formula.html}}/>:<span>{formula?.plain}</span>):
          item.codeBody?<pre>{block.export_modified?"修改后的代码\n":""}{block.code}{block.export_output?`\n\n运行结果\n${block.export_output}`:""}</pre>:
          <ProfessionalFigure graphic={block.graphic}/>}
      </div>;
    })}
  </section>;
}

export default function BoardExportView(){
  const [payload,setPayload]=useState<any>(null),[pages,setPages]=useState<any[]>([]),[error,setError]=useState("");
  const root=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    let cancelled=false;
    void window.desktopClassroom?.readExport().then(async value=>{
      await document.fonts.load("16px Cascadia");await document.fonts.ready;
      setBoardFontFamily("Cascadia, Segoe UI Emoji");
      (window as any).EXCALIDRAW_ASSET_PATH=new URL("/board-assets/",location.origin).href;
      const excalidraw=await import("@excalidraw/excalidraw");
      const output=[];
      for(const scene of value.scenes){
        const blocks=scene.presentation?.blocks||[];
        const prepared=blocks.map((block:any)=>block.type==="code"?{...block,
          execution_height:Math.max(80,(String(block.code).split("\n").length+
            String(block.export_output||"").split("\n").length+4)*22)}:block);
        const widths=[(1100-68)/3,(1100-68)*2/3+20,1072];
        const formulas=Object.fromEntries(prepared.filter((b:any)=>b.type==="formula").map((b:any)=>[b.id,measureFormula(b,widths,true)]));
        const geometry=layoutBoard(prepared,1100,new Set(prepared.map((b:any)=>b.id)),[],{},scene.presentation?.title||value.title,{},
          {responsive:true,pageHeight:1450,groups:boardGroups(prepared,scene.beats||[]),formulas});
        for(const page of geometry.pages){
          const elements=excalidraw.restore({elements:presentBoard(page,prepared)},null,null).elements;
          const svg=await excalidraw.exportToSvg({elements,files:null,appState:{exportBackground:false},exportPadding:0});
          svg.setAttribute("width","1100");svg.setAttribute("height",String(page.height));
          output.push({scene,page,svg:svg.outerHTML});
        }
      }
      if(!cancelled){setPayload(value);setPages(output);}
    }).catch(()=>{if(!cancelled)setError("板书导出准备失败。");});
    return()=>{cancelled=true;};
  },[]);
  useEffect(()=>{
    if(!pages.length||!root.current)return;
    let cancelled=false;
    const wait=async()=>{
      const deadline=performance.now()+25000;
      while(!cancelled){
        const figures=Array.from(root.current!.querySelectorAll(".professional-figure"));
        const missing=figures.some(node=>!node.querySelector(".function-plot-host svg,.surface-plot-host canvas,.circuit-diagram"));
        const images=Array.from(root.current!.querySelectorAll("img"));
        if(root.current!.querySelector(".figure-error")||images.some(image=>image.complete&&!image.naturalWidth))
          throw Error("asset");
        if(!missing&&images.every(image=>image.complete)){
          await document.fonts.ready;
          for(const canvas of root.current!.querySelectorAll("canvas")){
            const image=new Image();image.src=canvas.toDataURL("image/png");
            image.style.cssText=canvas.style.cssText;image.width=canvas.width;image.height=canvas.height;
            canvas.replaceWith(image);await image.decode();
          }
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          if(!cancelled)await window.desktopClassroom?.exportReady();
          return;
        }
        if(performance.now()>deadline)throw Error("timeout");
        await new Promise(resolve=>setTimeout(resolve,100));
      }
    };
    void wait().catch(()=>{if(!cancelled)setError("图形或原图未完整加载，导出已停止。");});
    return()=>{cancelled=true;};
  },[pages]);
  return <div className="board-export-document" ref={root}>
    {error&&<p role="alert">{error}</p>}
    {pages.map((item,index)=><ExportPage key={index} {...item}/>)}
    {payload?.scenes.flatMap((scene:any,index:number)=>(scene.presentation?.blocks||[]).flatMap((block:any)=>
      (block.export_images||[]).map((image:string,number:number)=><figure key={`code:${index}:${block.id}:${number}`}>
        <img src={image} alt="代码运行结果"/><figcaption>{block.title||"代码运行结果"}</figcaption></figure>)))}
    {payload?.scenes.flatMap((scene:any,index:number)=>(scene.original_refs||[]).map((ref:any)=>
      <figure key={`${index}:${ref.material_id}:${ref.figure_id}`}><img src={api.figureUrl(ref.material_id,ref.figure_id)} alt={ref.caption||"资料原图"}/><figcaption>{ref.caption}{ref.page_no?` · 原文第 ${ref.page_no} 页`:""}</figcaption></figure>))}
  </div>;
}
