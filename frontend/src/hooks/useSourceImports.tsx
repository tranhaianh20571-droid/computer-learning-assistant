import {useCallback,useEffect,useRef,useState} from "react";
import {FileText,Paperclip,X,LoaderCircle} from "lucide-react";
import {sourceApi} from "../api";
import Dialog from "../ui/Dialog";

export type ComposerFile={id:string;file:File;preview?:string};
type Chapter={url:string;title:string;depth:number};
function cancelledSelection(){
  return Object.assign(new Error("已取消章节选择。"),{code:"SOURCE_IMPORT_CANCELLED"});
}
export function useSourceImports(owner:string,onError:(message:string)=>void){
  const [files,setFiles]=useState<ComposerFile[]>([]);
  const [links,setLinks]=useState<string[]>([]);
  const [progress,setProgress]=useState<any>(null);
  const [working,setWorking]=useState(false);
  const [consent,setConsent]=useState<any>(null);
  const [preview,setPreview]=useState<{name:string;image?:string;text?:string;frames?:any[];sourceUrl?:string}|null>(null);
  const confirm=useRef<((value:boolean)=>void)|null>(null);
  const alive=useRef(true);
  const operation=useRef(false);
  const batch=useRef<{id:string;fingerprint:string}|null>(null);
  const selections=useRef(new Map<string,string[]>());
  const [directory,setDirectory]=useState<{url:string;chapters:Chapter[];truncated:boolean;loading:boolean}|null>(null);
  const [selected,setSelected]=useState<string[]>([]);
  const choose=useRef<((value:string[]|null)=>void)|null>(null);
  const discoveryAbort=useRef<AbortController|null>(null);
  const chapterPanel=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    const dialog=chapterPanel.current?.closest<HTMLElement>('[role="dialog"]');
    if(!dialog)return;
    const previous=document.activeElement as HTMLElement|null;
    const controls=()=>Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)'));
    controls()[0]?.focus();
    function trap(event:KeyboardEvent){
      if(event.key!=="Tab")return;
      const nodes=controls(),first=nodes[0],last=nodes[nodes.length-1];
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
    }
    dialog.addEventListener("keydown",trap);
    return ()=>{dialog.removeEventListener("keydown",trap);previous?.focus();};
  },[directory]);
  const currentFiles=useRef(files);
  useEffect(()=>{currentFiles.current=files;},[files]);
  useEffect(()=>{
    alive.current=true;
    const saved=owner?sessionStorage.getItem(`source-batch:${owner}`):null;
    if(saved)sourceApi.get(saved).then(value=>{if(alive.current){setProgress(value);batch.current={id:saved,fingerprint:"restored"};}}).catch(()=>{});
    return ()=>{alive.current=false;confirm.current?.(false);choose.current?.(null);discoveryAbort.current?.abort();
      currentFiles.current.forEach(x=>{if(x.preview)URL.revokeObjectURL(x.preview);});};
  },[owner]);
  const addFiles=useCallback((added:File[])=>{
    if(operation.current)return;
    if(added.some(file=>!/\.(pdf|docx?|pptx|txt|md|markdown|png|jpe?g|webp|mp4|webm|mov|mkv)$/i.test(file.name))){
      onError("支持 PDF、Word、PPTX、TXT、Markdown、图片和视频文件。");return;
    }
    setFiles(previous=>{
      if(previous.length+added.length+links.length>10){onError("每次最多附上 10 份资料。");return previous;}
      return [...previous,...added.map(file=>({id:crypto.randomUUID(),file,
        ...(file.type.startsWith("image/")?{preview:URL.createObjectURL(file)}:{})}))];
    });
  },[links.length,onError]);
  async function ingest(){
    if(operation.current)throw new Error("资料正在处理中。");
    if(!files.length&&!links.length){
      if(progress?.status==="ready")return progress.material_ids as string[];
      if(!progress)return [];
    }
    operation.current=true;setWorking(true);
    try{
      // Restored/failing batches reuse the frozen selection without rediscovery.
      const existingItems=[...links.map(url=>({client_id:"link_"+links.indexOf(url),url,
        ...(selections.current.get(url)?.length?{chapter_urls:selections.current.get(url)}:{})})),
        ...files.map(item=>({client_id:item.id,name:item.file.name,mime:item.file.type}))];
      const reusing=batch.current&&(batch.current.fingerprint===JSON.stringify(existingItems)
        ||(!existingItems.length&&batch.current.fingerprint==="restored"));
      if(!reusing){
        for(const url of links){
          if(selections.current.has(url))continue;
          const controller=new AbortController();
          discoveryAbort.current=controller;
          setDirectory({url,chapters:[],truncated:false,loading:true});
          let found:any;
          try{found=await sourceApi.directory(url,controller.signal);}
          catch(failure){
            if(controller.signal.aborted)throw cancelledSelection();
            throw failure;
          }finally{discoveryAbort.current=null;setDirectory(null);}
          if(!alive.current)throw cancelledSelection();
          if(found.chapters?.length){
            setSelected([]);
            const choice=await new Promise<string[]|null>(resolve=>{
              choose.current=resolve;setDirectory({...found,url,loading:false});
            });
            setDirectory(null);choose.current=null;
            if(!choice)throw cancelledSelection();
            selections.current.set(url,choice);
          }else selections.current.set(url,[]);
        }
      }
      const items=[...links.map(url=>({client_id:"link_"+links.indexOf(url),url,
        ...(selections.current.get(url)?.length?{chapter_urls:selections.current.get(url)}:{})})),
        ...files.map(item=>({client_id:item.id,name:item.file.name,mime:item.file.type}))];
      const fingerprint=JSON.stringify(items);
      let value:any;
      if(batch.current&&(batch.current.fingerprint===fingerprint||(!items.length&&batch.current.fingerprint==="restored"))){
        value=await sourceApi.get(batch.current.id);
      }else{
        value=await sourceApi.create({idempotency_key:crypto.randomUUID(),items});
        batch.current={id:value.batch_id,fingerprint};
        sessionStorage.setItem(`source-batch:${owner}`,value.batch_id);
        setProgress(value);
        for(const item of value.items){
          const local=files.find(file=>file.id===item.client_id);
          if(local){value=await sourceApi.upload(value.batch_id,item.item_id,local.file);if(alive.current)setProgress(value);}
        }
      }
      if(value.status==="draft"||value.status==="prepared"){
        const prepared=await sourceApi.prepare(value.batch_id);
        if(prepared.destinations.length){
          const allowed=await new Promise<boolean>(resolve=>{confirm.current=resolve;setConsent(prepared);});
          setConsent(null);confirm.current=null;
          if(!allowed)throw new Error("已取消资料处理，尚未向解析服务发送内容。");
        }
        value=await sourceApi.commit(value.batch_id,prepared.manifest_hash);
      }
      while(alive.current){
        setProgress(value);
        if(value.status==="ready")return value.material_ids as string[];
        if(value.status==="cancelled")throw new Error("资料处理已取消。");
        if(value.items.some((item:any)=>item.status==="failed"))
          throw new Error("部分资料处理失败，请查看状态并重试；尚未生成学习内容。");
        await new Promise(resolve=>setTimeout(resolve,1500));
        if(!alive.current)break;
        value=await sourceApi.get(value.batch_id);
      }
      throw new Error("资料仍在后台处理，返回后可恢复。");
    }catch(failure){
      if(failure instanceof Error)Object.assign(failure,{sourceImport:true});
      throw failure;
    }finally{operation.current=false;if(alive.current){setWorking(false);setDirectory(null);}}
  }
  function clear(){
    files.forEach(x=>{if(x.preview)URL.revokeObjectURL(x.preview);});
    setFiles([]);setLinks([]);setProgress(null);batch.current=null;
    selections.current.clear();
    sessionStorage.removeItem(`source-batch:${owner}`);
  }
  const picker=useRef<HTMLInputElement>(null);
  const pickerControl=<>
    <input ref={picker} type="file" className="sr" multiple
      accept=".pdf,.doc,.docx,.pptx,.txt,.md,.markdown,.png,.jpg,.jpeg,.webp,.mp4,.webm,.mov,.mkv"
      onChange={event=>{addFiles(Array.from(event.target.files||[]));event.target.value="";}}/>
    <button className="iconbtn" type="button" aria-label="选择本地资料" title="选择本地资料"
      disabled={working} onClick={()=>picker.current?.click()}><Paperclip size={20}/></button>
  </>;
  const tray=<>
    {!!files.length&&<ul className="source-files">{files.map(item=><li className="source-file" key={item.id}>
      {item.preview?<button type="button" className="iconbtn" title="预览图片" onClick={()=>setPreview({name:item.file.name,image:item.preview})}><img src={item.preview} alt={item.file.name}/></button>:<FileText size={26}/>}
      <span className="source-file-copy"><span className="source-file-name" title={item.file.name}>{item.file.name}</span>
      <small>{item.file.size<1024?`${item.file.size} B`:`${(item.file.size/1024).toFixed(0)} KB`}</small></span>
      <button type="button" className="iconbtn source-file-remove" title="移除附件" aria-label={`移除 ${item.file.name}`}
        disabled={working} onClick={()=>{if(item.preview)URL.revokeObjectURL(item.preview);setFiles(previous=>previous.filter(x=>x.id!==item.id));}}>
        <X size={14}/></button></li>)}</ul>}
    {progress&&<div className="source-import-progress" role="status">
      <ul>{progress.items.map((item:any)=><li key={item.item_id}>
        {working&&item.status!=="ready"&&<LoaderCircle size={12}/>} {item.name||item.title||item.url||"资料"} · {
          item.status==="ready"?"已可用":item.status==="failed"?"处理失败："+(item.failure_code||"未知错误"):
          item.status==="uploaded"?"等待确认":item.status==="pending"?"等待上传":
          item.question_extraction_status==="processing"?"题目整理中":"处理中"}
        {item.visual_coverage_complete===false&&" · 画面为采样识别"}
        {!!item.web_total&&` · 章节 ${item.web_completed||0}/${item.web_total}`}
        {!!item.review_reasons?.length&&" · 部分内容需核对"}
        {item.status==="ready"&&<button type="button" className="btn btn-sm" onClick={async()=>{
          try{
            if(item.kind==="document"){
              window.open(`/api/attachments/${encodeURIComponent(item.material_id)}/markdown`,"_blank","noopener,noreferrer");return;
            }
            const value=await sourceApi.material(item.material_id);
            const result=value.result;
            const text=(result.page_results||[]).flatMap((page:any)=>(page.blocks||[]).map((block:any)=>block.content||"")).join("\n\n");
            const auxiliary=(result.chunks||[]).filter((chunk:any)=>chunk.kind==="image_understanding").map((chunk:any)=>chunk.text).join("\n\n");
            setPreview({name:result.title||"资料识别稿",text:text+(auxiliary?"\n\n图像辅助解释（非逐字原文）\n"+auxiliary:""),
              frames:result.assets,sourceUrl:result.source_url});
          }catch{onError("识别稿读取失败，资料可能已删除。");}
        }}>查看识别稿</button>}
      </li>)}</ul>
      {progress.items.some((item:any)=>item.status==="failed")&&<button type="button" className="btn btn-sm"
        disabled={working} onClick={async()=>{
          const allowed=await new Promise<boolean>(resolve=>{confirm.current=resolve;setConsent(progress);});
          setConsent(null);confirm.current=null;
          if(!allowed)return;
          try{
            setProgress(await sourceApi.retry(progress.batch_id,progress.manifest_hash));
            await ingest();
          }catch(failure:any){onError(failure.message||"重试未完成。");}
        }}>重试失败资料</button>}
      {progress.status!=="ready"&&<button type="button" className="btn btn-sm" onClick={async()=>{
        await sourceApi.cancel(progress.batch_id);clear();
      }}>取消资料处理</button>}
    </div>}
    {directory&&<Dialog title={directory.loading?"正在发现目录":"选择章节"} kind="info"
      onClose={()=>directory.loading?discoveryAbort.current?.abort():choose.current?.(null)} actions={<>
        <button type="button" className="btn" onClick={()=>directory.loading?discoveryAbort.current?.abort():choose.current?.(null)}>取消</button>
        {!directory.loading&&<>
          <button type="button" className="btn" onClick={()=>choose.current?.([])}>仅导入当前页</button>
          <button type="button" className="btn btn-pri" disabled={!selected.length}
            onClick={()=>choose.current?.(selected)}>导入所选章节</button>
        </>}
      </>}>
      <div ref={chapterPanel}>{directory.loading?<p role="status"><LoaderCircle size={16}/> 正在读取网页目录…</p>:<>
        <div className="source-chapter-toolbar">
          <label><input type="checkbox" aria-label="全选章节" checked={selected.length===directory.chapters.length}
            ref={node=>{if(node)node.indeterminate=selected.length>0&&selected.length<directory.chapters.length;}}
            onChange={event=>setSelected(event.target.checked?directory.chapters.map(chapter=>chapter.url):[])}/>全选</label>
          <span role="status">已选 {selected.length} / {directory.chapters.length} 页</span>
        </div>
        {directory.truncated&&<p role="status">目录超过 300 页，仅列出前 300 页。</p>}
        <div className="source-chapter-list">{directory.chapters.map((chapter,index)=>{
          const children:Chapter[]=[chapter];
          for(let next=index+1;next<directory.chapters.length&&directory.chapters[next].depth>chapter.depth;next++)
            children.push(directory.chapters[next]);
          return <label key={chapter.url} style={{paddingLeft:Math.min(chapter.depth,4)*12+4}}>
            <input type="checkbox" checked={children.every(child=>selected.includes(child.url))}
              ref={node=>{if(node){const count=children.filter(child=>selected.includes(child.url)).length;
                node.indeterminate=count>0&&count<children.length;}}}
              onChange={event=>setSelected(previous=>{
                const urls=new Set(previous);
                children.forEach(child=>event.target.checked?urls.add(child.url):urls.delete(child.url));
                return directory.chapters.filter(child=>urls.has(child.url)).map(child=>child.url);
              })}/><span>{chapter.title}</span>
          </label>;
        })}</div>
      </>}</div>
    </Dialog>}
    {consent&&<Dialog title="确认处理资料" kind="info" onClose={()=>confirm.current?.(false)} actions={<>
      <button className="btn" onClick={()=>confirm.current?.(false)}>取消</button>
      <button className="btn btn-pri" onClick={()=>confirm.current?.(true)}>确认并处理</button>
    </>}>
      <p>将处理本次所附的 {consent.items.length} 份资料，以下服务可能接收网页地址、文件、音视频及识别内容，并产生用量。</p>
      {consent.items.some((item:any)=>item.chapter_count>0)&&<p>所选网页共 {consent.items.reduce((total:number,item:any)=>total+(item.chapter_count||0),0)} 页。</p>}
      <ul>{consent.destinations.map((destination:any)=><li key={destination.config_id}>
        {destination.purpose==="semantic"?"题目整理":destination.purpose==="visual"?"图形复核":
          destination.purpose==="model"?"多模态模型":destination.purpose==="document"?"文档识别":"资料解析"} · {destination.endpoint} · v{destination.config_version}
      </li>)}</ul>
    </Dialog>}
    {preview&&<Dialog title={preview.name} kind="info" onClose={()=>setPreview(null)}>
      {preview.image?<img className="source-preview-image" src={preview.image} alt={preview.name}/>:<pre className="source-preview-text">{preview.text}</pre>}
      {!!preview.frames?.length&&<div className="source-preview-frames">{preview.frames.map(frame=><figure key={frame.index}>
        <img loading="lazy" src={frame.url} alt={typeof frame.time_ms==="number"?`视频 ${Math.floor(frame.time_ms/1000)} 秒截图`:"网页图片"}/>
        {typeof frame.time_ms==="number"&&<figcaption>{preview.sourceUrl?
          <a href={(()=>{const target=new URL(preview.sourceUrl!);target.searchParams.set("t",String(Math.floor(frame.time_ms/1000)));return target.href;})()}
            target="_blank" rel="noopener noreferrer">原片 {Math.floor(frame.time_ms/1000)} 秒</a>:`${Math.floor(frame.time_ms/1000)} 秒`}</figcaption>}
      </figure>)}</div>}
    </Dialog>}
  </>;
  return {addFiles,setLinks,files,links,working,ingest,clear,pickerControl,tray,
    hasSources:!!(files.length||links.length||progress),progress};
}
