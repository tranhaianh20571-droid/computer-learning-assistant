// Synthetic toolchain diagnosis in fresh, explicitly isolated component roots.
import fs from "node:fs/promises";
import path from "node:path";
import {createRequire} from "node:module";
import {spawnSync} from "node:child_process";
const require=createRequire(import.meta.url),{download,extract}=require("../desktop/src/install.cjs");
const root=path.resolve(import.meta.dirname,".."),manifest=JSON.parse(await fs.readFile(path.join(root,"desktop/manifests/runtime.json")));
await fs.mkdir(path.join(root,".backup"),{recursive:true});
const work=await fs.mkdtemp(path.join(root,".backup/native-probe-")),roots={};
for(const id of ["core","classroom"]){
 const item=manifest.components.find(item=>item.id===id);
 const archive=path.join(root,".backup/desktop-components",path.basename(new URL(item.url).pathname));
 await download(item,archive,()=>{});
 roots[id]=path.join(work,id);await fs.mkdir(roots[id]);
 await extract(archive,roots[id],item.unpackedBytes);
}
for(const label of ["ascii","中文工作区"]){
 const result=spawnSync(path.join(roots.core,"python.exe"),[path.join(root,"tests/desktop_native_probe.py"),
  "--root",roots.classroom,"--work",path.join(work,label)],
  {cwd:root,windowsHide:true,encoding:"utf8",timeout:600000,maxBuffer:128*1024});
 console.log(label,result.status,result.stdout,result.stderr);
}
