import {createHash} from "node:crypto";
import {createRequire} from "node:module";
import {dirname,join} from "node:path";
import {readFile,writeFile,mkdir} from "node:fs/promises";
import {fileURLToPath} from "node:url";
const require=createRequire(import.meta.url);
const root=dirname(fileURLToPath(import.meta.url));
const runtime=dirname(require.resolve("pyodide/package.json"));
const cache=join(root,"node_modules",".cache","classroom-python");
let prepared;

export function prepareRuntime(){
  return prepared??=(async()=>{
    const lock=JSON.parse(await readFile(join(runtime,"pyodide-lock.json"),"utf8"));
    const selected=new Set();
    const add=name=>{if(selected.has(name))return;selected.add(name);lock.packages[name].depends.forEach(add);};
    ["numpy","matplotlib"].forEach(add);
    const assets=new Map();
    for(const name of ["pyodide.js","pyodide.asm.js","pyodide.asm.wasm","python_stdlib.zip","pyodide-lock.json"])
      assets.set(name,join(runtime,name));
    await mkdir(cache,{recursive:true});
    for(const name of selected){
      const pkg=lock.packages[name],path=join(cache,pkg.file_name);
      const valid=async()=>{try{return createHash("sha256").update(await readFile(path)).digest("hex")===pkg.sha256;}catch{return false;}};
      if(!await valid()){
        const response=await fetch(`https://cdn.jsdelivr.net/pyodide/v0.27.7/full/${pkg.file_name}`,
          {signal:AbortSignal.timeout(120000)});
        if(!response.ok)throw new Error(`Python package download failed: ${name}`);
        const bytes=Buffer.from(await response.arrayBuffer());
        if(createHash("sha256").update(bytes).digest("hex")!==pkg.sha256)throw new Error(`Python package checksum failed: ${name}`);
        await writeFile(path,bytes);
      }
      assets.set(pkg.file_name,path);
    }
    return assets;
  })();
}

export default function runtimeAssets(){
  return {name:"classroom-python-assets",
    async configureServer(server){
      const files=await prepareRuntime();
      server.middlewares.use("/python-runtime",async(req,res,next)=>{
        const name=req.url?.split("?")[0]?.slice(1);
        if(name==="manifest.json"){
          res.setHeader("Content-Type","application/json");
          res.end(JSON.stringify([...files.keys()]));return;
        }
        if(!files.has(name))return next();
        res.setHeader("Content-Type","application/octet-stream");
        res.setHeader("Cache-Control","public, max-age=3600");
        res.end(await readFile(files.get(name)));
      });
    },
    async generateBundle(){
      const files=await prepareRuntime();
      this.emitFile({type:"asset",fileName:"python-runtime/manifest.json",source:JSON.stringify([...files.keys()])});
      for(const [name,path] of files)this.emitFile({type:"asset",fileName:`python-runtime/${name}`,source:await readFile(path)});
    },
  };
}

if(process.argv[1]&&fileURLToPath(import.meta.url)===process.argv[1]){
  const assets=await prepareRuntime();
  console.log(`Prepared ${assets.size} verified Python runtime assets.`);
}
