// Application-only releases reuse immutable, hash-pinned runtime components.
import fs from "node:fs/promises";
import path from "node:path";
import {createRequire} from "node:module";
const require=createRequire(import.meta.url);
const {validateManifest,download}=require("../src/install.cjs");
const root=path.resolve(import.meta.dirname,"../..");
const output=path.join(root,".backup/desktop-components");
const manifest=validateManifest(JSON.parse(await fs.readFile(path.join(root,"desktop/manifests/runtime.json"),"utf8")));
const cache=process.argv[2]?path.resolve(process.argv[2]):null;
await fs.mkdir(output,{recursive:true});
for(const item of manifest.components){
  const destination=path.join(output,path.basename(new URL(item.url).pathname));
  if(cache){
    const source=path.join(cache,item.sha256+".zip");
    if(await fs.stat(source).catch(()=>null)){
      await fs.link(source,destination).catch(async error=>{
        if(error.code!=="EEXIST")await fs.copyFile(source,destination);
      });
    }
  }
  await download(item,destination,()=>{});
  console.log(`Verified pinned runtime: ${item.id} (${item.bytes} bytes)`);
}
await fs.copyFile(path.join(root,"desktop/manifests/runtime.json"),path.join(output,"runtime.json"));
