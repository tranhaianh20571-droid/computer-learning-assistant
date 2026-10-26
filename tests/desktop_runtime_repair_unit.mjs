import fs from "node:fs/promises";
import path from "node:path";
import assert from "node:assert/strict";
import {createRequire} from "node:module";
const require=createRequire(import.meta.url),yauzl=require("../desktop/node_modules/yauzl");
const root=path.resolve(import.meta.dirname,"..");
async function entries(file){
 return new Promise((resolve,reject)=>yauzl.open(file,{lazyEntries:true},(error,zip)=>{
  if(error)return reject(error);
  const rows=new Map();
  zip.on("error",reject);
  zip.on("entry",entry=>{assert(!rows.has(entry.fileName));rows.set(entry.fileName,{crc:entry.crc32,bytes:entry.uncompressedSize});zip.readEntry();});
  zip.on("end",()=>resolve(rows));zip.readEntry();
 }));
}
const manifest=JSON.parse(await fs.readFile(path.join(root,"desktop/manifests/runtime.json")));
for(const [id,changed] of [["postgres",["initdb","postgres","pg_ctl","pg_dump","pg_restore","pg_isready"].map(name=>`bin/${name}.exe`)],
 ["classroom",["jdk/bin/java.exe","jdk/bin/javac.exe"]]]){
 const component=manifest.components.find(item=>item.id===id);
 const before=await entries(path.join(root,`.backup/desktop-components/${id}-0.1.0-win32-x64.zip`));
 const after=await entries(path.join(root,".backup/desktop-components",path.basename(new URL(component.url).pathname)));
 let preserved=0;
 for(const [name,entry] of before){
  assert(after.has(name),`Missing original entry: ${name}`);
  if(!changed.includes(name)){assert.deepEqual(after.get(name),entry,`Unexpected component change: ${name}`);preserved++;}
 }
 const added=[...after.keys()].filter(name=>!before.has(name));
 assert.deepEqual(added,["STUDY-UTF8-NOTICE.txt"]);
 console.log(`${id}: ${preserved} original entries preserved; ${changed.length} launcher manifests changed`);
}
