import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {readFile,readdir} from "node:fs/promises";
import {createHash} from "node:crypto";
import path from "node:path";
const require=createRequire(import.meta.url),asar=require("../desktop/node_modules/@electron/asar");
assert(process.argv[2],"Select the installed directory explicitly");
const install=path.resolve(process.argv[2]),root=path.resolve(import.meta.dirname,"..");
const digest=value=>createHash("sha256").update(value).digest("hex");
const checked=[];
async function files(directory){
 const result=[];
 for(const entry of await readdir(directory,{withFileTypes:true})){
  if(entry.name==="__pycache__")continue;
  const filename=path.join(directory,entry.name);
  if(entry.isDirectory())result.push(...await files(filename));
  else if(!entry.name.endsWith(".pyc"))result.push(filename);
 }
 return result;
}
for(const [source,target] of [["server/backend/app","resources/backend/app"],["frontend/dist","resources/frontend"]]){
 const directory=path.join(root,source);
 for(const file of await files(directory)){
  const relative=path.relative(directory,file);
  assert.equal(digest(await readFile(file)),digest(await readFile(path.join(install,target,relative))),source+"/"+relative);
  checked.push(source+"/"+relative);
 }
}
for(const file of await files(path.join(root,"desktop/src"))){
 const relative=path.relative(path.join(root,"desktop"),file).replaceAll("\\","/");
 assert.equal(digest(await readFile(file)),digest(asar.extractFile(path.join(install,"resources/app.asar"),relative)),relative);
 checked.push(relative);
}
console.log(JSON.stringify({passed:true,matchingFiles:checked.length}));
