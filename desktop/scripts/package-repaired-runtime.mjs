// Preserve untouched archive entries; update only explicitly selected launchers.
import fs from "node:fs/promises";
import {createReadStream} from "node:fs";
import path from "node:path";
import {createHash} from "node:crypto";
import {createRequire} from "node:module";
import {execFileSync} from "node:child_process";
const require=createRequire(import.meta.url);
const {download}=require("../src/install.cjs");
const root=path.resolve(import.meta.dirname,"../.."),output=path.join(root,".backup/desktop-components");
const repairs={
 postgres:{revision:"18.1.0-utf8.1",patch:"patch-postgres.py",
  files:["initdb","postgres","pg_ctl","pg_dump","pg_restore","pg_isready"].map(name=>`bin/${name}.exe`),
  source:{id:"postgres",version:"0.1.0",bytes:62425297,unpackedBytes:176181173,
 sha256:"ea2394a6a9d9cee42176fa99f3ad74ff4cd34574ce11d34fbd13824e04a3fa4c",
 url:"https://github.com/tranhaianh20571-droid/computer-learning-assistant/releases/download/v0.1.0/postgres-0.1.0-win32-x64.zip"}},
 classroom:{revision:"0.1.0-java-utf8.1",patch:"patch-java.py",files:["jdk/bin/java.exe","jdk/bin/javac.exe"],
  source:{id:"classroom",version:"0.1.0",bytes:614114618,unpackedBytes:1776461385,
   sha256:"2aadfaf78e81d4918d0697074dd3ffa02d2e1ab3c74c5f6f5ef0bfae023ce9fd",
   url:"https://github.com/tranhaianh20571-droid/computer-learning-assistant/releases/download/v0.1.0/classroom-0.1.0-win32-x64.zip"}}
};
const id=process.argv[2]||"postgres",repair=repairs[id];
if(!repair)throw Error("Unsupported component repair");
const {source,revision,files}=repair,tag=`runtime-${id}-${revision}`;
const filename=`${id}-${revision}-win32-x64.zip`,destination=path.join(output,filename);
if(await fs.stat(destination).catch(()=>null))throw Error("Component revision already exists; do not overwrite it");
await fs.mkdir(output,{recursive:true});
const archive=path.join(output,path.basename(new URL(source.url).pathname));
await download(source,archive,()=>{});
const builds=path.join(root,".backup/desktop-build");
await fs.mkdir(builds,{recursive:true});
const work=await fs.mkdtemp(path.join(builds,id+"-utf8-")),seven=require("7zip-bin").path7za;
execFileSync(seven,["x","-y",archive,...files,"-o"+work],{windowsHide:true,stdio:"ignore"});
const before=await Promise.all(files.map(file=>fs.stat(path.join(work,file)).then(stat=>stat.size)));
execFileSync(process.env.BUILD_PYTHON||"python",[path.join(root,"desktop/scripts",repair.patch),work],
 {cwd:root,windowsHide:true,stdio:"inherit"});
const notice="STUDY-UTF8-NOTICE.txt",text=
 `This package derives from the pinned ${id} runtime archive.\n`+
 "Study Companion adds UTF-8 Windows process manifests to these launchers:\n"+
 files.join("\n")+"\nOriginal licenses and all other entries remain unchanged.\n"+
 "Windows 10 version 1903 or later is required for this process code-page setting.\n";
await fs.writeFile(path.join(work,notice),text);
await fs.copyFile(archive,destination);
execFileSync(seven,["u","-tzip","-mx=3",destination,...files,notice],
 {cwd:work,windowsHide:true,stdio:"ignore"});
const hash=createHash("sha256");
for await(const chunk of createReadStream(destination))hash.update(chunk);
const bytes=(await fs.stat(destination)).size;
const after=await Promise.all(files.map(file=>fs.stat(path.join(work,file)).then(stat=>stat.size)));
const component={id,version:revision,
 url:`https://github.com/tranhaianh20571-droid/computer-learning-assistant/releases/download/${tag}/${filename}`,
 bytes,unpackedBytes:source.unpackedBytes+after.reduce((sum,size,index)=>sum+size-before[index],0)+Buffer.byteLength(text),
 sha256:hash.digest("hex")};
const manifestPath=path.join(root,"desktop/manifests/runtime.json");
const manifest=JSON.parse(await fs.readFile(manifestPath,"utf8"));
manifest.version=JSON.parse(await fs.readFile(path.join(root,"desktop/package.json"),"utf8")).version;
manifest.components=manifest.components.map(item=>item.id===id?component:item);
await fs.writeFile(manifestPath,JSON.stringify(manifest,null,2)+"\n");
await fs.copyFile(manifestPath,path.join(output,"runtime.json"));
await fs.writeFile(path.join(output,filename+".sha256"),component.sha256+"  "+filename+"\n");
console.log(JSON.stringify({tag,...component}));
