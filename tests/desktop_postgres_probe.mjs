// Explicit isolated startup diagnosis, no existing database or user credentials.
import fs from "node:fs/promises";
import path from "node:path";
import {spawnSync} from "node:child_process";
import {createRequire} from "node:module";
const require=createRequire(import.meta.url);
const {validateManifest,download,extract}=require("../desktop/src/install.cjs");
const root=path.resolve(import.meta.dirname,"..");
const manifest=validateManifest(JSON.parse(await fs.readFile(path.join(root,"desktop/manifests/runtime.json"))));
const item=manifest.components.find(component=>component.id==="postgres");
const archive=path.join(root,".backup/desktop-components",path.basename(new URL(item.url).pathname));
await download(item,archive,()=>{});
const work=await fs.mkdtemp(path.join(root,".backup/pg-probe-"));
const runtime=path.join(work,"runtime");await fs.mkdir(runtime);
await extract(archive,runtime,item.unpackedBytes);
const binary=path.join(runtime,"bin/initdb.exe");
const results=[];
for(const [name,environment] of [
 ["ascii-minimal",false],["中文目录",false],["ascii-inherited",true],
]){
 const home=path.join(work,name);await fs.mkdir(home);
 const env=environment?{...process.env}:{SystemRoot:process.env.SystemRoot,WINDIR:process.env.WINDIR,
  PATH:path.join(process.env.SystemRoot,"System32"),USERPROFILE:home,LOCALAPPDATA:path.join(home,"local")};
 await fs.mkdir(path.join(home,"tmp"));env.TEMP=env.TMP=path.join(home,"tmp");
 const result=spawnSync(binary,["-D",path.join(home,"data"),"-U","synthetic_probe","--auth=trust","--encoding=UTF8","--locale=C"],
  {env,cwd:home,windowsHide:true,timeout:60000,encoding:"utf8",maxBuffer:256*1024});
 const diagnostic=(result.stderr||"").replaceAll(work,"<probe>").replaceAll(root,"<workspace>").slice(0,6000);
 results.push({name,status:result.status,signal:result.signal,error:result.error?.code,stderr:diagnostic});
 console.log(JSON.stringify(results.at(-1)));
}
await fs.writeFile(path.join(root,".backup/postgres-probe-result.json"),JSON.stringify(results,null,2));
