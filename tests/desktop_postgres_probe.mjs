// Explicit isolated startup diagnosis, no existing database or user credentials.
import fs from "node:fs/promises";
import path from "node:path";
import {spawnSync} from "node:child_process";
import {createRequire} from "node:module";
import assert from "node:assert/strict";
const require=createRequire(import.meta.url);
const {validateManifest,download,extract}=require("../desktop/src/install.cjs");
const {protectHome}=require("../desktop/src/paths.cjs");
const root=path.resolve(import.meta.dirname,"..");
const manifest=validateManifest(JSON.parse(await fs.readFile(path.join(root,"desktop/manifests/runtime.json"))));
const item=manifest.components.find(component=>component.id==="postgres");
const archive=path.join(root,".backup/desktop-components",path.basename(new URL(item.url).pathname));
await download(item,archive,()=>{});
const work=await fs.mkdtemp(path.join(root,".backup/pg-probe-"));
const runtime=path.join(work,"runtime");await fs.mkdir(runtime);
await extract(archive,runtime,item.unpackedBytes);
if(process.env.STUDY_PATCH_POSTGRES==="1"){
 const result=spawnSync(process.env.TEST_PYTHON||"python",["desktop/scripts/patch-postgres.py",runtime],
  {cwd:root,windowsHide:true,encoding:"utf8",timeout:60000});
 if(result.status!==0)throw Error("PostgreSQL manifest patch failed: "+result.stderr);
 console.log(result.stdout.trim());
}
const unicodeRuntime=path.join(work,"程序目录");
await fs.cp(runtime,unicodeRuntime,{recursive:true});
const results=[];
for(const [name,environment,program,protectedHome] of [
 ["ascii-minimal",false,runtime,false],["中文目录",false,runtime,false],["ascii-inherited",true,runtime,false],
 ["unicode-program",false,unicodeRuntime,false],["protected-home",false,runtime,true],
 ["中文完整路径",false,unicodeRuntime,true],
]){
 const home=path.join(work,name);await fs.mkdir(home);
 if(protectedHome)await protectHome(home);
 const env=environment?{...process.env}:{SystemRoot:process.env.SystemRoot,WINDIR:process.env.WINDIR,
  PATH:path.join(process.env.SystemRoot,"System32"),USERPROFILE:home,LOCALAPPDATA:path.join(home,"local")};
 await fs.mkdir(path.join(home,"tmp"));env.TEMP=env.TMP=path.join(home,"tmp");
 const result=spawnSync(path.join(program,"bin/initdb.exe"),["-D",path.join(home,"data"),"-U","synthetic_probe","--auth=trust","--encoding=UTF8","--locale=C"],
  {env,cwd:home,windowsHide:true,timeout:60000,encoding:"utf8",maxBuffer:256*1024});
 const diagnostic=(result.stderr||"").replaceAll(work,"<probe>").replaceAll(root,"<workspace>").slice(0,6000);
 results.push({name,status:result.status,signal:result.signal,error:result.error?.code,stderr:diagnostic});
 console.log(JSON.stringify(results.at(-1)));
}
await fs.writeFile(path.join(root,".backup/postgres-probe-result.json"),JSON.stringify(results,null,2));
if(process.env.STUDY_PATCH_POSTGRES==="1")assert(results.every(result=>result.status===0),"Patched PostgreSQL path checks failed");
