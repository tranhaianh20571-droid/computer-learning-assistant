export type RunResult={status:"success"|"error"|"stopped";output:string;images:string[]};

// This function is serialized into an opaque-origin frame, then into its Worker.
function sandboxWorker(){
  const scope=self as any;
  let python:any,assets:Record<string,ArrayBuffer>={},urls:Record<string,string>={};
  const nativeFetch=scope.fetch.bind(scope),nativeImport=scope.importScripts.bind(scope);
  const emit=scope.postMessage.bind(scope);
  let output="",images:string[]=[];
  const append=(value:any)=>{output=(output+String(value)+"\n").slice(0,32768);};
  scope.fetch=(input:any,options:any)=>{
    const key=String(input).split("/").pop();
    if(key&&assets[key])return Promise.resolve(new Response(assets[key],
      {headers:{"Content-Type":key.endsWith(".wasm")?"application/wasm":"application/octet-stream"}}));
    return nativeFetch(input,options);
  };
  scope.importScripts=(...paths:string[])=>nativeImport(...paths.map(path=>urls[path.split("/").pop()||""]||path));
  scope.onmessage=async(event:MessageEvent)=>{
    const data=event.data;
    if(data.type==="assets"){
      assets=data.assets;
      for(const [name,value] of Object.entries(assets)){
        if(name.endsWith(".js"))urls[name]=URL.createObjectURL(new Blob([value],{type:"text/javascript"}));
      }
      try{
        scope.importScripts(urls["pyodide.js"]);
        python=await scope.loadPyodide({indexURL:"https://runtime.invalid/",stdout:()=>{},stderr:()=>{}});
        await python.loadPackage(["numpy","matplotlib"],{messageCallback:()=>{},errorCallback:()=>{}});
        await python.runPythonAsync("import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt");
        emit({type:"assets-ready"});
      }catch(error:any){emit({type:"assets-error",message:String(error?.message||"运行环境加载失败").slice(0,1000)});}
      return;
    }
    if(data.type!=="run")return;
    output="";images=[];
    try{
      if(data.language==="python"){
        if(!python)throw new Error("Python 运行环境尚未就绪。");
        python.setStdout({batched:append});python.setStderr({batched:append});
        const value=await python.runPythonAsync(data.code);
        if(value!==undefined){append(String(value));value?.destroy?.();}
        const plots=python.runPython(`
import io as _classroom_io, base64 as _classroom_b64
_classroom_images = []
for _classroom_number in plt.get_fignums()[:4]:
    _classroom_buffer = _classroom_io.BytesIO()
    plt.figure(_classroom_number).savefig(_classroom_buffer, format="png", dpi=90)
    if _classroom_buffer.tell() <= 1500000:
        _classroom_images.append(_classroom_b64.b64encode(_classroom_buffer.getvalue()).decode())
plt.close('all')
_classroom_images
`);
        images=plots.toJs().map((image:string)=>"data:image/"+"png;base64,"+image);plots.destroy();
      }else{
        scope.console={log:(...values:any[])=>append(values.map(value=>typeof value==="string"?value:JSON.stringify(value)).join(" ")),
          error:append,warn:append,info:append};
        const value=await (0,eval)(data.code);
        if(value!==undefined)append(typeof value==="object"?JSON.stringify(value):value);
      }
      emit({type:"result",status:"success",output,images});
    }catch(error:any){
      append(String(error?.message||"运行失败").slice(0,8000));
      emit({type:"result",status:"error",output,images:[]});
    }
  };
}

function sandboxFrame(workerSource:string){
  let worker:Worker|null=null,port:MessagePort;
  self.addEventListener("message",(event:MessageEvent)=>{
    if(event.source!==parent||!event.ports[0]||port)return;
    port=event.ports[0];
    port.onmessage=event=>{
      const value=event.data;
      if(value.type==="reset"){worker?.terminate();worker=null;port.postMessage({type:"reset"});return;}
      if(!worker){
        const url=URL.createObjectURL(new Blob([workerSource],{type:"text/javascript"}));
        worker=new Worker(url);URL.revokeObjectURL(url);
        worker.onmessage=event=>port.postMessage(event.data);
        worker.onerror=()=>port.postMessage({type:"fatal"});
      }
      worker.postMessage(value,value.type==="assets"?Object.values(value.assets) as ArrayBuffer[]:[]);
    };
    port.postMessage({type:"ready"});
  });
}

let runtimePromise:Promise<Record<string,ArrayBuffer>>|undefined;
async function runtimeAssets(){
  return runtimePromise??=(async()=>{
    const base=new URL(`${import.meta.env.BASE_URL}python-runtime/`,location.href);
    const response=await fetch(new URL("manifest.json",base),{credentials:"omit",signal:AbortSignal.timeout(60000)});
    if(!response.ok)throw new Error("Python 运行环境加载失败，请重试。");
    const names=await response.json();
    if(!Array.isArray(names)||names.length>40||names.some(name=>typeof name!=="string"||!/^[A-Za-z0-9_.+-]+$/.test(name)))
      throw new Error("Python 运行环境清单无效。");
    return Object.fromEntries(await Promise.all(names.map(async(name:string)=>{
      const result=await fetch(new URL(name,base),{credentials:"omit",signal:AbortSignal.timeout(60000)});
      if(!result.ok)throw new Error("Python 运行环境加载失败，请重试。");
      return [name,await result.arrayBuffer()];
    })));
  })().catch(error=>{runtimePromise=undefined;throw error;});
}

export class CodeSandbox{
  frame:HTMLIFrameElement;
  port:MessagePort;
  ready:Promise<void>;
  pending:((value:RunResult)=>void)|null=null;
  assetsReady:(()=>void)|null=null;
  assetsError:((error:Error)=>void)|null=null;
  pythonLoaded=false;
  disposed=false;
  generation=0;
  constructor(){
    this.frame=document.createElement("iframe");
    this.frame.setAttribute("sandbox","allow-scripts");
    this.frame.setAttribute("aria-hidden","true");this.frame.hidden=true;
    this.frame.referrerPolicy="no-referrer";
    const worker=`(${sandboxWorker.toString()})()`;
    const script=`(${sandboxFrame.toString()})(${JSON.stringify(worker)})`;
    this.frame.srcdoc=`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:; worker-src blob:; connect-src blob:; img-src data: blob:;"><script>${script.replace(/<\/script/gi,"<\\/script")}<\/script>`;
    const channel=new MessageChannel();this.port=channel.port1;
    this.ready=new Promise(resolve=>{
      this.port.onmessage=event=>{
        const value=event.data;
        if(value?.type==="ready")resolve();
        if(value?.type==="assets-ready")this.assetsReady?.();
        if(value?.type==="assets-error")this.assetsError?.(new Error("Python 运行环境初始化失败。"));
        if(value?.type==="fatal"){this.stop();return;}
        if(value?.type==="result"&&this.pending){
          const finish=this.pending;this.pending=null;
          finish({status:value.status==="success"?"success":"error",
            output:String(value.output||"").slice(0,32768),
            images:Array.isArray(value.images)?value.images.filter((item:any)=>typeof item==="string"&&
              item.length<2000000&&/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(item)).slice(0,4):[]});
        }
      };
      this.frame.onload=()=>this.frame.contentWindow?.postMessage({type:"connect"},"*",[channel.port2]);
    });
    document.body.append(this.frame);
  }
  async run(code:string,language:string):Promise<RunResult>{
    if(this.disposed||this.pending)throw new Error("运行环境暂不可用。");
    const generation=this.generation;
    await this.ready;
    if(generation!==this.generation)return {status:"stopped",output:"运行已停止。",images:[]};
    if(language==="python"&&!this.pythonLoaded){
      const assets=await runtimeAssets();
      if(this.disposed||generation!==this.generation)return {status:"stopped",output:"运行已停止。",images:[]};
      await new Promise<void>((resolve,reject)=>{
        const timer=setTimeout(()=>{this.stop();reject(new Error("Python 运行环境加载超时，可重试。"));},90000);
        this.assetsReady=()=>{clearTimeout(timer);resolve();};
        this.assetsError=error=>{clearTimeout(timer);reject(error);};
        this.port.postMessage({type:"assets",assets});
      });
      if(generation!==this.generation)return {status:"stopped",output:"运行已停止。",images:[]};
      this.pythonLoaded=true;
    }
    if(this.disposed)throw new Error("运行已停止。");
    return new Promise(resolve=>{
      const timer=window.setTimeout(()=>this.stop("运行超时，变量环境已重置。"),15000);
      this.pending=value=>{clearTimeout(timer);resolve(value);};
      this.port.postMessage({type:"run",code,language});
    });
  }
  stop(message="运行已停止，变量环境已重置。"){
    this.generation++;
    this.port.postMessage({type:"reset"});this.pythonLoaded=false;
    this.assetsReady?.();this.assetsReady=null;
    const finish=this.pending;this.pending=null;
    finish?.({status:"stopped",output:message,images:[]});
  }
  dispose(){this.disposed=true;this.stop();this.port.close();this.frame.remove();}
}
