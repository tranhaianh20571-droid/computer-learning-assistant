// Rebuild only the PostgreSQL component from the immutable original archive.
import fs from "node:fs/promises";
import {createReadStream} from "node:fs";
import path from "node:path";
import {createHash} from "node:crypto";
import {createRequire} from "node:module";
import {execFileSync} from "node:child_process";
const require=createRequire(import.meta.url);
const {download,extract}=require("../src/install.cjs");
const root=path.resolve(import.meta.dirname,"../.."),output=path.join(root,".backup/desktop-components");
const revision="18.1.0-utf8.1",tag="runtime-postgres-"+revision;
const source={id:"postgres",version:"0.1.0",bytes:62425297,unpackedBytes:176181173,
 sha256:"ea2394a6a9d9cee42176fa99f3ad74ff4cd34574ce11d34fbd13824e04a3fa4c",
 url:"https://github.com/tranhaianh20571-droid/computer-learning-assistant/releases/download/v0.1.0/postgres-0.1.0-win32-x64.zip"};
await fs.mkdir(output,{recursive:true});
const archive=path.join(output,path.basename(new URL(source.url).pathname));
await download(source,archive,()=>{});
const builds=path.join(root,".backup/desktop-build");
await fs.mkdir(builds,{recursive:true});
const work=await fs.mkdtemp(path.join(builds,"postgres-utf8-"));
await extract(archive,work,source.unpackedBytes);
execFileSync(process.env.BUILD_PYTHON||"python",[path.join(root,"desktop/scripts/patch-postgres.py"),work],
 {cwd:root,windowsHide:true,stdio:"inherit"});
await fs.writeFile(path.join(work,"STUDY-UTF8-NOTICE.txt"),
 "This package derives from the pinned PostgreSQL 18.1 runtime archive.\n"+
 "Study Companion adds UTF-8 Windows process manifests to initdb, postgres, pg_ctl,\n"+
 "pg_dump, pg_restore and pg_isready. Original licenses and binaries otherwise remain.\n"+
 "Windows 10 version 1903 or later is required for this process code-page setting.\n");
const filename=`postgres-${revision}-win32-x64.zip`,destination=path.join(output,filename);
if(await fs.stat(destination).catch(()=>null))throw Error("Component revision already exists; do not overwrite it");
execFileSync(require("7zip-bin").path7za,["a","-tzip","-mx=3",destination,"."],
 {cwd:work,windowsHide:true,stdio:"ignore"});
const hash=createHash("sha256");
for await(const chunk of createReadStream(destination))hash.update(chunk);
const bytes=(await fs.stat(destination)).size;
const sizes=await Promise.all((await fs.readdir(work,{recursive:true,withFileTypes:true}))
 .filter(entry=>entry.isFile()).map(entry=>fs.stat(path.join(entry.parentPath,entry.name)).then(stat=>stat.size)));
const component={id:"postgres",version:revision,
 url:`https://github.com/tranhaianh20571-droid/computer-learning-assistant/releases/download/${tag}/${filename}`,
 bytes,unpackedBytes:sizes.reduce((sum,size)=>sum+size,0),sha256:hash.digest("hex")};
const manifestPath=path.join(root,"desktop/manifests/runtime.json");
const manifest=JSON.parse(await fs.readFile(manifestPath,"utf8"));
manifest.version=JSON.parse(await fs.readFile(path.join(root,"desktop/package.json"),"utf8")).version;
manifest.components=manifest.components.map(item=>item.id==="postgres"?component:item);
await fs.writeFile(manifestPath,JSON.stringify(manifest,null,2)+"\n");
await fs.copyFile(manifestPath,path.join(output,"runtime.json"));
await fs.writeFile(path.join(output,filename+".sha256"),component.sha256+"  "+filename+"\n");
console.log(JSON.stringify({tag,...component}));
