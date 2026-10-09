import {useCallback,useEffect,useLayoutEffect,useMemo,useRef,useState} from "react";
import {ArrowDown} from "lucide-react";
import {layoutBoard} from "../lib/boardLayout";
import type {Block,GroupAnchor} from "../lib/boardLayout";
import {boardGroups} from "../lib/boardGroups.ts";
import {BoardMotion} from "../lib/boardMotion.ts";
import {boardWritingY} from "../lib/boardWriting.ts";
import {boardFontFamily,setBoardFontFamily} from "../lib/boardText.ts";
import {presentBoard,stableElements} from "../lib/boardPresentation.ts";
import {measureFormula,renderFormulaBlock} from "../lib/formula.ts";
import type {FormulaMeasurement} from "../lib/formula.ts";
import "katex/dist/katex.min.css";

export default function TeachingBoard({scene,sceneKey,presenting=true}:{scene:any;sceneKey:string;presenting?:boolean}) {
  const viewport=useRef<HTMLDivElement>(null),canvasApi=useRef<any>(null);
  const [loaded,setLoaded]=useState<any>(null),[error,setError]=useState("");
  const [width,setWidth]=useState(1000),[viewportHeight,setViewportHeight]=useState(620),[page,setPage]=useState(0);
  const [focus,setFocus]=useState<Record<string,string>>({}),[fontRevision,setFontRevision]=useState(0);
  const [canvasView,setCanvasView]=useState({zoom:1,scrollX:12,scrollY:10});
  const [formulaMetrics,setFormulaMetrics]=useState<Record<string,FormulaMeasurement>>({});
  const [following,setFollowing]=useState(true),[away,setAway]=useState(false);
  const anchors=useRef<Record<string,GroupAnchor>>({}),anchorKey=useRef(""),fitKey=useRef("");
  const layoutRevisions=useRef(0),mounted=useRef(false),forceFollow=useRef(false);
  const pointerStart=useRef<{x:number;y:number}|null>(null);
  const motion=useRef<BoardMotion|null>(null),manual=useRef(false);
  const resumeFollow=useRef<()=>void>(()=>{}),refreshAway=useRef<()=>void>(()=>{});
  const suspendFollow=useCallback(()=>{manual.current=true;forceFollow.current=false;motion.current?.stop();setFollowing(false);},[]);
  const elementCache=useRef(new Map<string,{signature:string;element:any}>()),lastScene=useRef<any[]>([]);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;}},[]);
  useEffect(()=>{
    let cancelled=false;
    Promise.all([import("@excalidraw/excalidraw"),import("@excalidraw/excalidraw/index.css")])
      .then(([mod])=>{if(!cancelled){setBoardFontFamily("Cascadia, Segoe UI Emoji");setLoaded(mod);}})
      .catch(()=>{if(!cancelled)setError("板书画布加载失败；文字讲解仍可阅读。");});
    return()=>{cancelled=true;};
  },[]);
  useEffect(()=>{
    if(!loaded)return;
    let cancelled=false;
    void document.fonts.load("16px Cascadia").then(()=>document.fonts.ready).then(()=>{if(!cancelled)setFontRevision(value=>value+1);})
      .catch(()=>{if(!cancelled){setBoardFontFamily("Consolas, Segoe UI Emoji");setFontRevision(value=>value+1);}});
    return()=>{cancelled=true;};
  },[loaded]);
  useLayoutEffect(()=>{
    const node=viewport.current;if(!node)return;
    const resize=()=>{setWidth(Math.max(240,node.clientWidth));setViewportHeight(node.clientHeight);};
    resize();const observer=new ResizeObserver(resize);observer.observe(node);
    return()=>observer.disconnect();
  },[]);
  useLayoutEffect(()=>{
    const node=viewport.current;if(!node||error)return;
    const reduced=window.matchMedia("(prefers-reduced-motion: reduce)");
    const controller=new BoardMotion(node,{request:callback=>window.requestAnimationFrame(callback),cancel:id=>window.cancelAnimationFrame(id)},active=>{
      node.dataset.boardMoving=String(active);if(!active)refreshAway.current();
    });
    motion.current=controller;controller.setReduced(reduced.matches);
    const preference=()=>{controller.setReduced(reduced.matches);};
    const visibility=()=>{controller.stop();if(!document.hidden)resumeFollow.current();};
    reduced.addEventListener("change",preference);document.addEventListener("visibilitychange",visibility);
    return()=>{controller.stop();motion.current=null;reduced.removeEventListener("change",preference);document.removeEventListener("visibilitychange",visibility);};
  },[error]);
  useLayoutEffect(()=>{motion.current?.stop();manual.current=false;setPage(0);setFocus({});setFollowing(true);fitKey.current="";forceFollow.current=true;},[sceneKey]);
  const presentation=scene?.presentation,visible:Block[]=presentation?.blocks||[],blocks:Block[]=presentation?.all_blocks||visible;
  const columns=width<640?1:width<1100?2:3,columnWidth=(width-28-(columns-1)*20)/columns;
  const formulaKey=JSON.stringify(blocks.filter(block=>block.type==="formula").map(block=>({id:block.id,formula:block.formula,format:block.format,plain_text:block.plain_text})));
  useLayoutEffect(()=>{
    let cancelled=false;
    const widths=Array.from({length:columns},(_,index)=>(index+1)*columnWidth+index*20);
    const measure=(refresh=false)=>{
      if(cancelled)return;
      const metrics=Object.fromEntries((JSON.parse(formulaKey) as Block[]).map(block=>[block.id,measureFormula(block,widths,refresh)]));
      setFormulaMetrics(previous=>JSON.stringify(previous)===JSON.stringify(metrics)?previous:metrics);
    };
    measure();void document.fonts.ready.then(()=>measure(true));
    return()=>{cancelled=true;};
  },[formulaKey,width,fontRevision]);
  const metricsKey=JSON.stringify(formulaMetrics);
  const groupKey=`${sceneKey}:${width}:${viewportHeight}:${fontRevision}:${blocks.map(block=>block.id).join(",")}:${metricsKey}`;
  if(anchorKey.current!==groupKey){anchorKey.current=groupKey;anchors.current={};}
  const serialized=JSON.stringify({blocks,focus,title:presentation?.title,beats:scene?.beats||[]});
  const geometry=useMemo(()=>{
    layoutRevisions.current++;
    return layoutBoard(blocks,width,new Set(blocks.map(block=>block.id)),[],focus,presentation?.title||"",{},
      {responsive:true,pageHeight:Math.max(240,viewportHeight-20),groups:boardGroups(blocks,scene?.beats||[]),anchors:anchors.current,formulas:formulaMetrics});
  },[serialized,width,viewportHeight,groupKey]);
  useEffect(()=>{
    const ids=new Set(visible.map(block=>block.id));
    for(const sheet of geometry.pages)for(const group of sheet.groups)
      if(group.block_ids.some(id=>ids.has(id))&&!anchors.current[group.id])anchors.current[group.id]={page:group.page,column:group.column,span:group.span,y:group.y};
  },[geometry,visible]);
  const selectedPage=Math.min(page,geometry.pages.length-1),sheet=geometry.pages[selectedPage];
  const visibleKey=JSON.stringify(visible);
  const elements=useMemo(()=>presentation?presentBoard(sheet,visible,presentation.highlight_ids||[]):scene?.elements||[],[geometry,selectedPage,visibleKey]);
  const initialData=useMemo(()=>({elements,appState:{viewBackgroundColor:"#fcfcfb",zoom:{value:1},scrollX:12,scrollY:10}}),[loaded]);
  const latest=useRef<any>({elements});latest.current={elements};
  const onCanvasChange=useCallback((_elements:any,state:any)=>{
    if(!mounted.current)return;
    const next={zoom:Number(state.zoom?.value)||1,scrollX:Number(state.scrollX)||0,scrollY:Number(state.scrollY)||0};
    setCanvasView(old=>old.zoom===next.zoom&&old.scrollX===next.scrollX&&old.scrollY===next.scrollY?old:next);
  },[]);
  const update=useCallback((api:any)=>{
    canvasApi.current=api;if(!api||!loaded)return;
    const restored=stableElements(latest.current.elements,elementCache.current,input=>loaded.restore({elements:input},null,null,{repairBindings:false,refreshDimensions:true}).elements);
    if(restored.length!==lastScene.current.length||restored.some((element,index)=>element!==lastScene.current[index])){
      lastScene.current=restored;api.updateScene({elements:restored});
    }
    const key=`${sceneKey}:${selectedPage}:${width}`;
    if(fitKey.current!==key){fitKey.current=key;api.updateScene({appState:{zoom:{value:1},scrollX:12,scrollY:10}});}
  },[loaded,sceneKey,selectedPage,width]);
  useEffect(()=>{if(canvasApi.current)update(canvasApi.current);},[elements,update]);
  useLayoutEffect(()=>{
    motion.current?.stop();
    const node=viewport.current;
    if(node){node.scrollTop=0;node.scrollLeft=0;}
    forceFollow.current=true;
  },[sceneKey,selectedPage]);

  const writing=presentation?.writing,activeBlock=visible.find(block=>block.id===writing?.block_id);
  const activePage=activeBlock?geometry.pages.findIndex(item=>item.placements.some(p=>p.id===activeBlock.id)):-1;
  const placement=activePage>=0?geometry.pages[activePage].placements.find(item=>item.id===activeBlock?.id):undefined;
  const writingY=placement&&activeBlock?boardWritingY(geometry.pages[activePage],placement,activeBlock,writing?.progress??1):0;
  const targetY=(writingY+canvasView.scrollY)*canvasView.zoom;
  const sheetHeight=Math.max(viewportHeight,Math.ceil((sheet.height+16+Math.max(0,canvasView.scrollY))*canvasView.zoom+viewportHeight*.55));
  const sheetWidth=Math.max(width,Math.ceil(geometry.width*canvasView.zoom)+12);
  const latestFollow=useRef({targetY,x:0,activePage,selectedPage,hasBlock:false,following,presenting});
  latestFollow.current={targetY,x:((placement?.x||0)+canvasView.scrollX)*canvasView.zoom,activePage,selectedPage,hasBlock:!!activeBlock,following,presenting};
  const center=useCallback((force=false)=>{
    const node=viewport.current,latest=latestFollow.current;if(!node||!latest.hasBlock)return;
    if(latest.activePage!==latest.selectedPage){motion.current?.stop();forceFollow.current=true;setPage(latest.activePage);return;}
    motion.current?.follow(latest.targetY,force);
    if(latest.x<node.scrollLeft||latest.x>node.scrollLeft+node.clientWidth-40)node.scrollLeft=Math.max(0,latest.x-18);
  },[]);
  refreshAway.current=()=>{
    const node=viewport.current,latest=latestFollow.current;if(!node)return;
    const relative=latest.targetY-node.scrollTop,x=latest.x-node.scrollLeft;
    const next=latest.hasBlock&&(latest.activePage!==latest.selectedPage||relative<node.clientHeight*.3||relative>node.clientHeight*.75||x<0||x>node.clientWidth-40);
    setAway(old=>old===next?old:next);
  };
  resumeFollow.current=()=>{const latest=latestFollow.current;if(latest.following&&!manual.current&&latest.presenting)center();};
  useEffect(()=>{refreshAway.current();},[targetY,activePage,selectedPage,width,viewportHeight,canvasView.scrollX,canvasView.zoom]);
  const writingKey=`${activePage}:${writing?.unit_id||activeBlock?.id||""}:${activeBlock?.id||""}`;
  useLayoutEffect(()=>{
    if(!following||manual.current||!presenting||!activeBlock||document.hidden){motion.current?.stop();return;}
    const force=forceFollow.current;forceFollow.current=false;center(force);
  },[writingKey,targetY,following,presenting,width,viewportHeight,selectedPage,activePage,center]);
  const zoom=(factor:number)=>{
    suspendFollow();const api=canvasApi.current;if(!api)return;
    api.updateScene({appState:{zoom:{value:Math.max(.2,Math.min(3,api.getAppState().zoom.value*factor))}}});
  };
  const byId=new Map(visible.map(block=>[block.id,block]));
  const graphs=visible.filter(block=>block.type==="graph"&&sheet.placements.some(p=>p.id===block.id));
  return <div className="teaching-board" data-board-pages={geometry.pages.length} data-layout-revisions={layoutRevisions.current}>
    <div className="board-controls">
      {geometry.pages.length>1&&<><button className="btn btn-sm" disabled={selectedPage===0} onClick={()=>{suspendFollow();setPage(value=>value-1);}}>上一页板书</button>
        <span>第 {selectedPage+1} / {geometry.pages.length} 页</span>
        <button className="btn btn-sm" disabled={selectedPage>=geometry.pages.length-1} onClick={()=>{suspendFollow();setPage(value=>value+1);}}>下一页板书</button></>}
      <button className="btn btn-sm" aria-label="缩小板书" onClick={()=>zoom(.85)}>−</button>
      <button className="btn btn-sm" aria-label="放大板书" onClick={()=>zoom(1.15)}>＋</button>
      <button className="btn btn-sm" onClick={()=>{suspendFollow();fitKey.current="";update(canvasApi.current);}}>适应画面</button>
    </div>
    {error?<div className="notice">{error}</div>:<div className="teaching-board-viewport">
      <div ref={viewport} className="teaching-board-canvas" aria-label="课程板书" tabIndex={0}
        data-board-elements={elements.length} data-visible-blocks={visible.map(block=>block.id).join(",")} data-board-scale={canvasView.zoom}
        data-layout={JSON.stringify(sheet.placements)} data-board-following={following?"true":"false"} data-writing-y={activeBlock?targetY:""}
        data-writing-id={activeBlock?.id||""} data-writing-page={activePage} data-board-page={selectedPage}
        onScroll={()=>refreshAway.current()}
        onPointerDownCapture={event=>{pointerStart.current={x:event.clientX,y:event.clientY};if(event.clientX>event.currentTarget.getBoundingClientRect().right-16)suspendFollow();}}
        onPointerMoveCapture={event=>{const start=pointerStart.current;if(start&&Math.hypot(event.clientX-start.x,event.clientY-start.y)>8)suspendFollow();}}
        onPointerUpCapture={()=>{pointerStart.current=null;}}
        onPointerCancelCapture={()=>{pointerStart.current=null;}}
        onTouchStartCapture={()=>suspendFollow()}
        onKeyDownCapture={event=>{if(["PageUp","PageDown","Home","End","ArrowUp","ArrowDown","ArrowLeft","ArrowRight"," "].includes(event.key))suspendFollow();}}
        onWheelCapture={event=>{suspendFollow();if(!event.ctrlKey&&!event.metaKey)event.stopPropagation();}}>
        <div style={{height:sheetHeight,width:sheetWidth}} className="teaching-board-sheet">
          {loaded?(()=>{const Canvas=loaded.Excalidraw;return <Canvas excalidrawAPI={update} viewModeEnabled gridModeEnabled={false} onChange={onCanvasChange} initialData={initialData}/>;})():<p className="muted">板书画布加载中…</p>}
          {sheet.placements.map(item=>{
            const block=byId.get(item.id),body=item.formulaBody||item.codeBody;if(!block||!body)return null;
            const rendered=item.formulaBody?renderFormulaBlock(block):null;
            return <div key={item.id} className="teaching-formula-overlay" data-formula-id={item.formulaBody?item.id:undefined} data-code-id={item.codeBody?item.id:undefined}
              style={{left:(body.x+canvasView.scrollX)*canvasView.zoom,top:(body.y+canvasView.scrollY)*canvasView.zoom,width:body.width,height:body.height,
                transform:`scale(${canvasView.zoom})`,fontSize:item.codeBody?14:20}}>
              {item.codeBody?<pre className="teaching-code-content" tabIndex={0} aria-label={block.title||"代码"} style={{fontFamily:boardFontFamily()}}
                onWheel={event=>{if(event.shiftKey||Math.abs(event.deltaX)>Math.abs(event.deltaY))event.stopPropagation();}}>{block.code}</pre>:
                <div className="teaching-formula-content" aria-label={rendered?.plain||"数学公式"} tabIndex={0} onWheel={event=>{if(event.shiftKey||Math.abs(event.deltaX)>Math.abs(event.deltaY))event.stopPropagation();}}>
                  {rendered?.valid?<span dangerouslySetInnerHTML={{__html:rendered.html}}/>:<span className="teaching-formula-fallback">{rendered?.plain}</span>}
                </div>}
            </div>;
          })}
        </div>
      </div>
      {away&&activeBlock&&<button className="board-return" aria-label="回到讲解位置" title="回到讲解位置"
        onClick={()=>{manual.current=false;setFollowing(true);forceFollow.current=true;if(presenting)center(true);}}><ArrowDown size={18}/></button>}
    </div>}
    {graphs.map(graph=><details key={graph.id} className="board-graph-detail">
      <summary>{graph.title||"关系图"} · 查看局部连接（共 {graph.edges?.length||0} 条）</summary>
      <div className="row"><button className="btn btn-sm" onClick={()=>setFocus(old=>({...old,[graph.id]:""}))}>整体视图</button>
        {graph.nodes.map((node:any)=><button key={node.id} className="btn btn-sm" aria-pressed={focus[graph.id]===node.id} onClick={()=>setFocus(old=>({...old,[graph.id]:node.id}))}>{node.label}</button>)}</div>
      {focus[graph.id]&&<ul>{graph.edges.filter((edge:any)=>edge.from_id===focus[graph.id]||edge.to_id===focus[graph.id]).map((edge:any,index:number)=><li key={index}>{graph.nodes.find((node:any)=>node.id===edge.from_id)?.label} → {graph.nodes.find((node:any)=>node.id===edge.to_id)?.label}{edge.label?`：${edge.label}`:""}</li>)}</ul>}
    </details>)}
  </div>;
}
