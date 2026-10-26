const {app,BrowserWindow,dialog,session,desktopCapturer}=require("electron");
const http=require("node:http"),fs=require("node:fs/promises"),path=require("node:path");
const {installClassroomMedia}=require("../desktop/src/classroom-media.cjs");
const output=path.resolve(".backup/paper-classroom");
const userData=path.join(output,"electron-"+process.pid);
require("node:fs").mkdirSync(userData,{recursive:true});
app.setPath("userData",userData);
const frontend=path.resolve(process.env.TEST_DIST||"frontend/dist");
const text="Synthetic classroom narration.".repeat(3);
function wav(frequency=440){
  const samples=48000*20,buffer=Buffer.alloc(44+samples*2);
  buffer.write("RIFF");buffer.writeUInt32LE(buffer.length-8,4);buffer.write("WAVEfmt ",8);
  buffer.writeUInt32LE(16,16);buffer.writeUInt16LE(1,20);buffer.writeUInt16LE(1,22);buffer.writeUInt32LE(48000,24);
  buffer.writeUInt32LE(96000,28);buffer.writeUInt16LE(2,32);buffer.writeUInt16LE(16,34);
  buffer.write("data",36);buffer.writeUInt32LE(samples*2,40);
  for(let i=0;i<samples;i++)buffer.writeInt16LE(Math.round(Math.sin(i*2*Math.PI*frequency/48000)*3000),44+i*2);
  return buffer;
}
let checkpoint={client_seq:1,step_no:1,position_ms:0,presentation:{visible_chars:0,paused:true}};
const records=new Map(),audio=wav(),followupAudio=wav(660),feynmanAudio=wav(880);
app.commandLine.appendSwitch("no-proxy-server");
app.commandLine.appendSwitch("autoplay-policy","no-user-gesture-required");
app.whenReady().then(async()=>{
 await fs.mkdir(output,{recursive:true});
 const server=http.createServer(async(req,res)=>{
  try{
   const target=new URL(req.url,"http://localhost"),pathname=target.pathname;
   if(pathname==="/api/auth/me"){res.setHeader("Content-Type","application/json");res.end(JSON.stringify({user_id:"synthetic-owner",status:"approved"}));return;}
   if(pathname.includes("/audio/")){res.setHeader("Content-Type","audio/wav");res.end(
    pathname.includes("followup")?followupAudio:pathname.includes("feynman")?feynmanAudio:audio);return;}
   if(pathname.startsWith("/api/")){
    const chunks=[];for await(const chunk of req)chunks.push(chunk);const bytes=Buffer.concat(chunks);
    const json=()=>bytes.length?JSON.parse(bytes):{};
    let value={data:[],saved:true};
    if(pathname==="/api/teaching/synthetic-desktop")value={
      conversation_id:"synthetic-desktop",course_id:"course",lesson_id:"lesson",lesson_title:"Synthetic recording",
      kind:"teaching",generation:1,session_state:"active",server_managed:true,playback_version:0,
      planned_steps:1,audio_mode:"tts",tts_authorized:true,followups:[],queued_tasks:[],
      steps:[{step_id:"step",step_no:1,title:"Synthetic lesson",text,status:"ready",text_version_no:1,
        lesson_finished:true,audio_id:"aud_synthetic",audio_ready:true,
        audio_subtitles:[{text,begin_ms:0,end_ms:20000}],board:{presentation:{version:2,title:"Synthetic board",blocks:[
          {id:"note",type:"text",text:"Reliable classroom recording"},
          {id:"formula",type:"formula",formula:"E=mc^2"},
          {id:"code",type:"code",language:"text",code:"result = 2 + 3\nprint(result)"},
          {id:"graph",type:"graph",diagram:"flowchart",nodes:[{id:"a",label:"Read"},{id:"b",label:"Explain"}],edges:[{from_id:"a",to_id:"b",label:"reason"}]}]}}}],
      last_checkpoint:checkpoint};
    if(pathname==="/api/teaching/synthetic-desktop"&&global.fixture?.mode==="followup"){
      value.session_state="awaiting_resume";value.generation=2;
      value.followups=[{generation:2,role:"assistant",content:"Synthetic follow-up narration."}];
      value.followup_board={presentation:{version:2,title:"Follow-up",blocks:[]}};
      value.answer_audio={audio_id:"aud_followup",subtitles:[{text:"Synthetic follow-up narration.",begin_ms:0,end_ms:20000}]};
    }
    if(pathname==="/api/teaching/synthetic-desktop"&&global.fixture?.mode==="feynman"){
      value.feynman={checkpoints:[],active:{session_id:"quiz",checkpoint_id:"point",after_step_no:1,revision:1,
       state:"waiting",scope:"knowledge",probe_count:0,max_probes:5,audio_required:true,
       history:[{id:"feynman-turn",role:"assistant",text:"Synthetic Feynman narration.",audio_id:"aud_feynman",
        subtitles:[{text:"Synthetic Feynman narration.",begin_ms:0,end_ms:20000}],playback:{ended:false}}]}};
    }
    if(pathname.endsWith("/checkpoint")||pathname.endsWith("/seek-backward")){const data=json();checkpoint={...data,presentation:data.board_state.presentation};}
    if(pathname.endsWith("/recordings")){
      if(req.method==="POST"){const id=records.size?`rec_synthetic_${records.size+1}`:"rec_synthetic";value={recording_id:id,created_at:new Date().toISOString(),status:"recording"};records.set(id,{...value,chunks:[]});}
      else value={data:Array.from(records.values()).map(({chunks,...row})=>({...row,chunks:chunks.length}))};
    }
    const match=pathname.match(/recordings\/([^/]+)\/chunks\/(\d+)/);
    if(match){
      const directory=path.join(output,process.env.RECORDING_TEST_DIR||"recording-chunks");
      await fs.mkdir(directory,{recursive:true});
      const file=path.join(directory,`${match[1]}-${String(match[2]).padStart(6,"0")}.webm`);
      await fs.writeFile(file,bytes);
      records.get(match[1]).chunks[Number(match[2])]=file;value={saved:true};
    }
    if(pathname.endsWith("/finish")){
      const id=pathname.split("/").at(-2),record=records.get(id);
      const name=process.env.RECORDING_TEST_OUTPUT||"recording.webm";
      const file=await fs.open(path.join(output,id==="rec_synthetic"?name:name.replace(".webm",`-${id}.webm`)),"w");
      try{for(const chunk of record.chunks)await file.write(await fs.readFile(chunk));}finally{await file.close();}
      record.status=json().complete===false?"interrupted":"ready";value={...record,chunks:record.chunks.length};
    }
    res.setHeader("Content-Type","application/json");res.end(JSON.stringify(value));return;
   }
   const filename=path.resolve(frontend,"."+decodeURIComponent(pathname==="/" ? "/index.html":pathname));
   if(!filename.startsWith(frontend+path.sep)){res.writeHead(404);res.end();return;}
   const mime={".html":"text/html",".js":"text/javascript",".mjs":"text/javascript",".css":"text/css",".woff2":"font/woff2",".woff":"font/woff",".ttf":"font/ttf",".png":"image/png",".svg":"image/svg+xml"};
   res.setHeader("Content-Type",mime[path.extname(filename)]||"application/octet-stream");res.end(await fs.readFile(filename));
  }catch{res.writeHead(500);res.end();}
 }).listen(0,"127.0.0.1",async()=>{
   const origin=`http://127.0.0.1:${server.address().port}`;
   const window=new BrowserWindow({show:true,width:1440,height:1000,
    webPreferences:{preload:path.resolve("desktop/src/preload.cjs"),contextIsolation:true,sandbox:true,nodeIntegration:false}});
   window.on("close",()=>console.log("Synthetic fixture window close requested"));
   window.webContents.on("render-process-gone",(_event,details)=>console.log("Synthetic renderer exited:",details.reason));
   dialog.showSaveDialog=async(_window,options)=>({canceled:false,filePath:path.join(output,options.defaultPath)});
   const handler=session.defaultSession.setDisplayMediaRequestHandler.bind(session.defaultSession);
   const captureCalls=[];
   session.defaultSession.setDisplayMediaRequestHandler=callback=>handler((request,complete)=>{
    captureCalls.push({frame:request.frame===window.webContents.mainFrame,securityOrigin:request.securityOrigin,
      origin,video:request.videoRequested,audio:request.audioRequested});
    callback(request,streams=>{captureCalls.at(-1).allowed=Boolean(streams?.video);complete(streams);});
   });
   installClassroomMedia({window,origin:()=>origin,applicationFrame:event=>{
    if(event.sender!==window.webContents||event.senderFrame!==window.webContents.mainFrame)throw Error("frame");
   }});
   await window.loadURL(origin+"/#/lecture/synthetic-desktop");
   global.fixture={window,server,records,captureCalls,sources:()=>desktopCapturer.getSources({types:["window"]})};
 });
});
app.on("window-all-closed",()=>app.quit());
