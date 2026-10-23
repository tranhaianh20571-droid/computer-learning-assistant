import {classroomApi} from "../api";
import type {RunResult} from "./codeSandbox";

export function containerLanguage(language:string,code:string){
  if(["java","c","cpp","pytorch"].includes(language))return language;
  return language==="python"&&/(?:^|[;\n])\s*(?:import\s+[^;\n]*\btorch\b|from\s+torch(?:\.|\s))/.test(code)?"pytorch":"";
}

export class ContainerCode{
  active:{conversation:string;id:string;controller:AbortController}|null=null;
  async run(conversation:string,body:unknown):Promise<RunResult>{
    const current={conversation,id:crypto.randomUUID(),controller:new AbortController()};
    this.active=current;
    try{
      const value=await classroomApi.run(conversation,current.id,body,current.controller.signal);
      return {status:["success","error","stopped"].includes(value.status)?value.status:"error",
        output:String(value.output||"").slice(0,32768),
        images:Array.isArray(value.images)?value.images.filter((v:unknown)=>typeof v==="string"&&v.length<2_000_000&&
          /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(v)).slice(0,4):[]};
    }catch(error:any){
      if(current.controller.signal.aborted)return {status:"stopped",output:"运行已停止。",images:[]};
      // A failed connection may still have submitted work; cancel, never retry execution.
      void classroomApi.stop(conversation,current.id).catch(()=>{});
      const messages:Record<string,string>={
        classroom_unavailable:"隔离运行环境不可用，请稍后重试。",
        classroom_busy:"运行环境忙，请稍后重试。",
        CHECKPOINT_STALE:"课堂版本已变化，请刷新后继续。",
      };
      return {status:"error",output:messages[error?.code]||"运行请求未完成，请重新运行。",images:[]};
    }finally{if(this.active===current)this.active=null;}
  }
  stop(){
    const current=this.active;
    if(!current)return;
    this.active=null;current.controller.abort();
    void classroomApi.stop(current.conversation,current.id).catch(()=>{});
  }
  dispose(){this.stop();}
}
