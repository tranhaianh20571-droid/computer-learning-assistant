import {useEffect,useLayoutEffect,useRef,useState} from "react";
import {createPortal} from "react-dom";
import {recordingApi} from "../api";
import {classroomAudioStream,releaseRecordingAudio,onClassroomNavigation,beforeClassroomExit} from "../lib/classroomMedia";

type Lecture={conversation:string;step:number;presenting:boolean;active:boolean;enabled:boolean};
let pendingSave:Promise<void>=Promise.resolve();

function ClassroomRecording({lecture,onErr}:{lecture:Lecture;onErr:(message:string)=>void}){
  const [revision,reconcile]=useState(0),mounted=useRef(false);
  const latest=useRef(lecture);
  useLayoutEffect(()=>{latest.current=lecture;},[lecture]);
  const recorder=useRef<MediaRecorder|null>(null),stream=useRef<MediaStream|null>(null);
  const task=useRef<Promise<void>|null>(null),stopping=useRef<Promise<void>|null>(null);
  const epoch=useRef(0),sequence=useRef(0),queuedBytes=useRef(0);
  const queue=useRef<Promise<unknown>>(Promise.resolve()),failed=useRef(false),failureShown=useRef(false);
  const clock=useRef({elapsed:0,since:0}),recording=useRef(""),events=useRef<{at_ms:number;step_no:number}[]>([]);
  const position=()=>Math.max(0,Math.floor(clock.current.elapsed+(clock.current.since?performance.now()-clock.current.since:0)));
  const report=()=>{if(!failureShown.current){failureShown.current=true;onErr("课堂录像暂时无法保存，学习可继续。复习时可重试恢复已录内容。");}};
  const stop=()=>{
    epoch.current++;
    if(stopping.current)return stopping.current;
    const media=recorder.current,id=recording.current,conversation=latest.current.conversation;
    if(!media||media.state==="inactive")return Promise.resolve();
    const ended=new Promise<void>(resolve=>media.addEventListener("stop",()=>resolve(),{once:true}));
    media.stop();stream.current?.getTracks().forEach(track=>track.stop());stream.current=null;
    const saved=ended.then(async()=>{
      releaseRecordingAudio();
      try{
        await queue.current.catch(()=>{failed.current=true;});
        if(sequence.current&&position()>=200)await recordingApi.finish(conversation,id,{duration_ms:position(),complete:!failed.current,events:events.current});
        else await recordingApi.remove(conversation,id);
      }catch{report();}
      finally{
        recorder.current=null;stopping.current=null;recording.current="";
        if(mounted.current)reconcile(value=>value+1);
      }
    });
    stopping.current=saved;
    pendingSave=saved;
    return saved;
  };
  const stopRef=useRef(stop);
  const start=async()=>{
    if(recorder.current||failed.current||!window.desktopClassroom)return;
    const token=epoch.current,conversation=latest.current.conversation;
    let video:MediaStream|null=null,created="";
    const current=()=>token===epoch.current&&latest.current.active&&latest.current.enabled;
    try{
      await pendingSave;
      if(!current())return;
      const previous=await recordingApi.list(conversation);
      for(const row of previous.data||[]){
        if(row.status!=="recording")continue;
        if(row.chunks)await recordingApi.finish(conversation,row.recording_id,{duration_ms:0,complete:false,events:[]});
        else await recordingApi.remove(conversation,row.recording_id);
      }
      if(!current())return;
      await window.desktopClassroom.allowCapture();
      video=await navigator.mediaDevices.getDisplayMedia({video:{frameRate:24},audio:false});
      if(!current())throw Error("cancelled");
      const audio=await classroomAudioStream();
      if(!current())throw Error("cancelled");
      const mime=["video/webm;codecs=vp8,opus","video/webm;codecs=vp9,opus","video/webm"].find(type=>MediaRecorder.isTypeSupported(type));
      if(!mime)throw Error("unsupported");
      const item=await recordingApi.start(conversation,mime);created=item.recording_id;
      if(!current())throw Error("cancelled");
      recording.current=created;sequence.current=0;queuedBytes.current=0;queue.current=Promise.resolve();
      clock.current={elapsed:0,since:0};events.current=[{at_ms:0,step_no:latest.current.step}];
      const mixed=new MediaStream([...video.getVideoTracks(),...audio.getAudioTracks()]);
      stream.current=mixed;
      const media=new MediaRecorder(mixed,{mimeType:mime,videoBitsPerSecond:1_800_000,audioBitsPerSecond:96_000});
      recorder.current=media;
      media.onstart=()=>{clock.current.since=performance.now();};
      media.onpause=()=>{clock.current.elapsed=position();clock.current.since=0;};
      media.onresume=()=>{clock.current.since=performance.now();};
      media.addEventListener("stop",()=>{clock.current.elapsed=position();clock.current.since=0;});
      media.ondataavailable=event=>{
        if(!event.data.size)return;
        const index=sequence.current++;queuedBytes.current+=event.data.size;
        if(queuedBytes.current>16*1024*1024&&media.state==="recording")media.pause();
        queue.current=queue.current.then(async()=>{
          let error:unknown;
          for(let attempt=0;attempt<3;attempt++){
            try{await recordingApi.chunk(conversation,created,index,event.data);error=null;break;}
            catch(value){error=value;}
          }
          if(error)throw error;
          queuedBytes.current-=event.data.size;
          if(queuedBytes.current<4*1024*1024&&latest.current.active&&latest.current.presenting&&media.state==="paused")media.resume();
        });
        void queue.current.catch(()=>{failed.current=true;report();void stopRef.current();});
      };
      media.onerror=()=>{failed.current=true;report();void stopRef.current();};
      video.getVideoTracks()[0].onended=()=>void stopRef.current();
      media.start(2000);
      if(!latest.current.presenting)media.pause();
      mixed.getVideoTracks().forEach(track=>{track.enabled=latest.current.presenting;});
    }catch{
      video?.getTracks().forEach(track=>track.stop());releaseRecordingAudio();
      if(created&&!recorder.current)await recordingApi.remove(conversation,created).catch(()=>{});
      if(current()){failed.current=true;report();}
    }
  };
  const startRef=useRef(start);
  useLayoutEffect(()=>{stopRef.current=stop;startRef.current=start;});
  useEffect(()=>{
    mounted.current=true;
    const remove=onClassroomNavigation(async exiting=>{
      const saving=stopRef.current();
      if(exiting)await saving;
    });
    return()=>{mounted.current=false;remove();void stopRef.current();};
  },[]);
  useEffect(()=>{
    if(lecture.active){failed.current=false;failureShown.current=false;}
  },[lecture.active]);
  useEffect(()=>{
    if(!lecture.active||!lecture.enabled){void stopRef.current();return;}
    const media=recorder.current;
    if(!media){
      if(!task.current&&!stopping.current&&!failed.current&&lecture.presenting){
        const starting=startRef.current();task.current=starting;
        void starting.finally(()=>{task.current=null;if(mounted.current)reconcile(value=>value+1);});
      }
      return;
    }
    stream.current?.getVideoTracks().forEach(track=>{track.enabled=lecture.presenting;});
    if(lecture.presenting&&media.state==="paused"&&queuedBytes.current<4*1024*1024)media.resume();
    else if(!lecture.presenting&&media.state==="recording")media.pause();
  },[lecture.active,lecture.enabled,lecture.presenting,revision]);
  useEffect(()=>{
    if(recorder.current&&events.current.length<4000)events.current.push({at_ms:position(),step_no:lecture.step});
  },[lecture.step]);
  return null;
}

export default function ClassroomRecordingHost(){
  const [lecture,setLecture]=useState<Lecture|null>(null),[error,setError]=useState("");
  useEffect(()=>{
    const close=window.desktopClassroom?.onBeforeClose(()=>beforeClassroomExit());
    const playback=(event:Event)=>setLecture({...((event as CustomEvent).detail),active:true});
    const leave=()=>setLecture(old=>old?{...old,presenting:false,active:false}:null);
    const expired=()=>{setLecture(null);setError("");};
    window.addEventListener("classroom-playback",playback);
    window.addEventListener("hashchange",leave);
    window.addEventListener("session-expired",expired);
    return()=>{
      close?.();window.removeEventListener("classroom-playback",playback);
      window.removeEventListener("hashchange",leave);window.removeEventListener("session-expired",expired);
    };
  },[]);
  return lecture?<><ClassroomRecording key={lecture.conversation} lecture={lecture} onErr={setError}/>
    {error&&lecture.active&&createPortal(<div role="alert" className="classroom-media-error">{error}
      <button aria-label="关闭录像提示" onClick={()=>setError("")}>×</button>
    </div>,document.getElementById("classroom-recording-slot")||document.body)}</>:null;
}
