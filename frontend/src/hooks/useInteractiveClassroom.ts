import {useCallback,useEffect,useRef,useState} from "react";
import {classroomApi} from "../api";
import {CodeSandbox} from "../lib/codeSandbox";
import type {RunResult} from "../lib/codeSandbox";
import {ContainerCode,containerLanguage} from "../lib/containerCode";

export type CodeCell={step_no:number;block_id:string;original_code:string;code:string;output:string;
  generation?:number;
  status:"idle"|"success"|"error"|"stopped";runs:number;phase:"run"|"change"|"done";images?:string[];
  baseline_code?:string;baseline_output?:string};
export const codeLanguage=(language:string)=>( {py:"python",python:"python",python3:"python",js:"javascript",javascript:"javascript",
  java:"java",c:"c",cpp:"cpp","c++":"cpp",cxx:"cpp",pytorch:"pytorch",torch:"pytorch"} as Record<string,string>)[language?.trim().toLowerCase()]||"";
export const cellKey=(step:number,block:string,generation=1)=>`${step}:${generation}:${block}`;

export function useInteractiveClassroom(conversation:string,version:number,enabled=true){
  const [cells,setCells]=useState<Record<string,CodeCell>>({}),[ready,setReady]=useState(false);
  const [running,setRunning]=useState(""),[error,setError]=useState(""),[environment,setEnvironment]=useState("");
  const [revision,setRevision]=useState(0);
  const [load,setLoad]=useState(0);
  const store=useRef<Record<string,CodeCell>>({}),rev=useRef(0),epoch=useRef(0),busy=useRef("");
  const operation=useRef(0);
  const remote=useRef<ContainerCode|null>(null);
  const engine=useRef<CodeSandbox|null>(null),saveQueue=useRef<Promise<any>>(Promise.resolve()),timer=useRef<number|undefined>(undefined);
  useEffect(()=>{
    const token=++epoch.current;setReady(false);setError("");store.current={};setCells({});rev.current=0;setRevision(0);
    busy.current="";setRunning("");setEnvironment("");engine.current?.dispose();engine.current=null;remote.current?.dispose();remote.current=null;
    if(!enabled){setReady(true);return;}
    classroomApi.get(conversation).then(state=>{
      if(epoch.current!==token)return;
      if(state.playback_version!==version){setError("课堂版本已变化，请刷新后继续。");return;}
      store.current=Object.fromEntries(state.cells.map((cell:CodeCell)=>[cellKey(cell.step_no,cell.block_id,cell.generation),cell]));
      rev.current=state.revision;setRevision(state.revision);setCells({...store.current});setReady(true);setError("");
      if(state.cells.some((cell:CodeCell)=>cell.runs>0))setEnvironment("变量环境已重置，请按需重新运行依赖代码。");
    }).catch(()=>{if(epoch.current===token)setError("交互记录加载失败，请重新加载。");});
    return()=>{epoch.current++;clearTimeout(timer.current);engine.current?.dispose();engine.current=null;remote.current?.dispose();remote.current=null;};
  },[conversation,version,enabled,load]);
  const save=useCallback(()=>{
    clearTimeout(timer.current);
    const token=epoch.current;
    const pending=async()=>{
      if(epoch.current!==token)return false;
      const data=Object.values(store.current).map(({images:_images,...cell})=>cell);
      try{
        const state=await classroomApi.save(conversation,{revision:rev.current,playback_version:version,cells:data});
        if(epoch.current!==token)return false;
        rev.current=state.revision;setRevision(state.revision);setError("");return true;
      }catch{if(epoch.current===token)setError("交互记录保存失败；请重试保存后再继续。");return false;}
    };
    saveQueue.current=saveQueue.current.then(pending,pending);
    return saveQueue.current as Promise<boolean>;
  },[conversation,version]);
  const update=useCallback((cell:CodeCell)=>{
    store.current={...store.current,[cellKey(cell.step_no,cell.block_id,cell.generation)]:cell};
    setCells(store.current);clearTimeout(timer.current);
    timer.current=window.setTimeout(()=>void save(),650);
  },[save]);
  const get=useCallback((step:number,block:any):CodeCell=>store.current[cellKey(step,block.id,block.source_generation||1)]||
    {step_no:step,generation:block.source_generation||1,block_id:block.id,original_code:block.code,
      code:block.code,output:"",status:"idle",runs:0,phase:"run"},[]);
  const run=useCallback(async(step:number,block:any)=>{
    if(busy.current||!ready)return;
    const key=cellKey(step,block.id,block.source_generation||1),cell=get(step,block),token=epoch.current;
    const runOperation=++operation.current;
    update(cell);
    busy.current=key;setRunning(key);setEnvironment("");
    const language=codeLanguage(block.language),container=containerLanguage(language,cell.code);
    if(container)setEnvironment("本次独立运行，不保留上次变量。"+(container==="pytorch"?"PyTorch 使用 CPU。":""));
    let result:RunResult;
    try{
      if(container){
        remote.current??=new ContainerCode();
        result=await remote.current.run(conversation,{playback_version:version,step_no:step,generation:cell.generation||1,
          block_id:block.id,original_code:cell.original_code,code:cell.code});
      }else{
        engine.current??=new CodeSandbox();
        result=await engine.current.run(cell.code,language);
      }
    }
    catch{result={status:"error",output:"运行环境加载失败，可重试。",images:[]};}
    if(epoch.current!==token||operation.current!==runOperation)return;
    const latest=store.current[key]||cell;
    // Output is useful only for the exact version that was executed.
    if(latest.code===cell.code)update({...latest,...result,runs:latest.runs+1});
    busy.current="";setRunning("");
    if(result.status==="stopped")setEnvironment("变量环境已重置，请按需重新运行依赖代码。");
    await save();
  },[get,ready,save,update,conversation,version]);
  const stop=useCallback(()=>{
    operation.current++;engine.current?.stop();remote.current?.stop();
    const cell=store.current[busy.current];
    if(cell){update({...cell,status:"stopped",output:"运行已停止，变量环境已重置。",images:[],runs:cell.runs+1});void save();}
    busy.current="";setRunning("");setEnvironment("变量环境已重置，请按需重新运行依赖代码。");
  },[update,save]);
  return {cells,ready,running,error,environment,revision,get,update,run,stop,save,
    currentRevision:()=>rev.current,reload:()=>setLoad(value=>value+1)};
}

export type ClassroomCode=ReturnType<typeof useInteractiveClassroom>;
