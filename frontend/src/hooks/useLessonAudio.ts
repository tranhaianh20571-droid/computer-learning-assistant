import {useEffect,useRef,useState} from "react";
import {api} from "../api";
import {audioCharacters,exactAudioAlignment} from "../lib/audioTiming.ts";
import {classroomAudio,releaseClassroomAudio} from "../lib/classroomMedia";

export function lessonAudioRequired(workspace:any,step:any,current:number){
  if(workspace?.tts_authorized===false || workspace?.audio_mode==="text")return false;
  const pending=workspace?.queued_tasks?.some((task:any)=>task.kind==="tts"&&task.step_no===current&&["queued","leased","running"].includes(task.status));
  return Boolean(workspace?.tts_authorized===true || workspace?.audio_mode==="tts" || (step?.audio_ready&&step.audio_id) || pending);
}

export const LESSON_AUDIO_WAIT_MS = 12_000;
type AudioState = "idle"|"playing"|"paused"|"blocked"|"ended"|"failed"|"waiting";

export function useLessonAudio(conversation:string,current:number,step:any,playing:boolean,bufferRef:any,posRef:any,resume:any,audioRequired=false,seekRequest?:{id:number;step_no:number;position_ms:number}|null){
  const media=useRef<HTMLAudioElement|null>(null),source=useRef(""),sourceEpoch=useRef(0);
  const waitingForOffset=useRef(false),appliedSeek=useRef<number|null>(null);
  const [state,setState]=useState<AudioState>("idle"),[retryCount,setRetryCount]=useState(0);
  const latest=useRef({step,current,playing,audioRequired});latest.current={step,current,playing,audioRequired};
  const subtitleKey=JSON.stringify(step?.audio_subtitles||[]);
  const gate=(blocked:boolean)=>bufferRef.current.audioGate(`step:${latest.current.current}`,blocked);
  const sync=()=>{
    const value=latest.current,audio=media.current;
    if(!audio)return;
    posRef.current=Math.floor(audio.currentTime*1000);
    bufferRef.current.audioClock(`step:${value.current}`,audioCharacters(
      value.step?.text||"",value.step?.audio_subtitles||[],posRef.current,value.step?.board?.alignment_version===1));
  };
  const play=()=>{
    const audio=media.current;
    if(!audio||!source.current||waitingForOffset.current||!latest.current.audioRequired||!latest.current.playing)return;
    const epoch=sourceEpoch.current;
    audio.play().then(()=>{
      if(epoch!==sourceEpoch.current)return;
      if(!latest.current.audioRequired||!latest.current.playing){audio.pause();return;}
      if(!audio.paused&&audio.readyState>=2){sync();gate(false);setState("playing");}
    }).catch(error=>{
      if(epoch!==sourceEpoch.current)return;
      gate(true);setState(error.name==="NotAllowedError"?"blocked":navigator.onLine?"failed":"waiting");
    });
  };
  useEffect(()=>{
    const audio=classroomAudio();audio.preload="auto";media.current=audio;
    let frame=0;
    const tick=()=>{
      if(source.current&&latest.current.playing&&!audio.paused&&!audio.seeking&&audio.readyState>=2)sync();
      frame=requestAnimationFrame(tick);
    };
    frame=requestAnimationFrame(tick);
    const waiting=()=>{if(source.current){gate(true);setState("waiting");}};
    audio.onwaiting=waiting;audio.onseeking=waiting;
    audio.onstalled=()=>{if(audio.readyState<3)waiting();};
    audio.onseeked=()=>{
      if(source.current&&latest.current.playing&&!audio.paused&&audio.readyState>=2){
        sync();gate(false);setState("playing");
      }
    };
    audio.onplaying=()=>{
      if(!source.current||!latest.current.playing||!latest.current.audioRequired){audio.pause();return;}
      sync();gate(false);setState("playing");
    };
    audio.oncanplay=()=>{if(latest.current.playing)play();};
    audio.onended=()=>{
      if(!source.current||!latest.current.playing||!latest.current.audioRequired)return;
      sync();
      gate(false);setState("ended");
      bufferRef.current.audioClock(`step:${latest.current.current}`,Array.from(latest.current.step?.text||"").length);
    };
    audio.onerror=()=>{if(source.current){sourceEpoch.current++;gate(true);setState(navigator.onLine?"failed":"waiting");}};
    return()=>{
      sourceEpoch.current++;cancelAnimationFrame(frame);
      audio.onplaying=null;audio.onwaiting=null;audio.onstalled=null;audio.onseeking=null;audio.onseeked=null;
      audio.oncanplay=null;audio.onended=null;audio.onerror=null;
      audio.pause();audio.removeAttribute("src");audio.load();releaseClassroomAudio(audio);media.current=null;source.current="";
    };
  },[conversation]);
  useEffect(()=>{
    const audio=media.current;if(!audio)return;
    sourceEpoch.current++;source.current="";audio.pause();gate(audioRequired);setState("idle");
    audio.removeAttribute("src");audio.load();
    if(audioRequired&&step?.audio_ready&&step.audio_id){
      const cues=step.audio_subtitles;
      if(step.board?.alignment_version===1&&!exactAudioAlignment(step.text||"",cues||[])){
        setState("failed");return;
      }
      if(!Array.isArray(cues)||!cues.length||!cues.every((cue:any)=>
        Number.isFinite(cue.begin_ms)&&Number.isFinite(cue.end_ms)&&cue.begin_ms>=0&&cue.end_ms>cue.begin_ms)){
        setState("failed");return;
      }
      source.current=step.audio_id;audio.src=api.audioUrl(step.audio_id);
      const explicitSeek=seekRequest?.step_no===current&&seekRequest.id!==appliedSeek.current;
      if(explicitSeek)appliedSeek.current=seekRequest!.id;
      const offset=explicitSeek?seekRequest!.position_ms:
        resume?.step_no===current&&resume?.presentation?.audio_id===step.audio_id?resume.position_ms:0;
      posRef.current=offset;
      let seeked=offset<=0;
      waitingForOffset.current=!seeked;
      const restorePosition=()=>{
        if(seeked)return;
        try{
          audio.currentTime=Number.isFinite(audio.duration)?Math.min(offset/1000,Math.max(0,audio.duration-.05)):offset/1000;
          seeked=true;waitingForOffset.current=false;
        }catch{/* The canplay event retries after the source becomes seekable. */}
      };
      audio.onloadedmetadata=()=>{restorePosition();play();};audio.oncanplay=()=>{restorePosition();play();};
      if(latest.current.playing)play();else setState("paused");
    }
    return()=>{sourceEpoch.current++;audio.pause();audio.onloadedmetadata=null;audio.oncanplay=null;};
  },[conversation,current,step?.audio_id,step?.audio_ready,audioRequired,subtitleKey,seekRequest?.id]);
  useEffect(()=>{
    if(!audioRequired){gate(false);bufferRef.current.audioClock(`step:${current}`,null);return;}
    if(!playing){media.current?.pause();gate(true);if(state!=="ended")setState("paused");}
    else if(state!=="ended")play();
  },[playing,audioRequired]);
  useEffect(()=>{
    if(audioRequired&&!step?.audio_ready&&["failed","invalid"].includes(step?.audio_status)){
      gate(true);setState("failed");
    }
  },[audioRequired,step?.audio_ready,step?.audio_status]);
  useEffect(()=>{
    if(!audioRequired||!playing||["playing","ended","failed","blocked"].includes(state))return;
    const timer=window.setTimeout(()=>{gate(true);setState("waiting");},LESSON_AUDIO_WAIT_MS);
    return()=>window.clearTimeout(timer);
  },[conversation,current,audioRequired,playing,state,retryCount]);
  return {state,textOnly:!audioRequired,sourceId:audioRequired?source.current:null,
    alignmentFailed:step?.board?.alignment_version===1&&step?.audio_ready&&!exactAudioAlignment(step.text||"",step.audio_subtitles||[]),
    pause:()=>{sourceEpoch.current++;if(media.current&&source.current)sync();media.current?.pause();gate(audioRequired);},
    enable:()=>{setRetryCount(value=>value+1);setState("idle");play();},
    retry:()=>{
      setRetryCount(value=>value+1);setState("idle");gate(audioRequired);
      if(source.current&&media.current){
        sourceEpoch.current++;media.current.load();
        const offset=posRef.current;
        media.current.onloadedmetadata=()=>{if(media.current)media.current.currentTime=offset/1000;};
        play();
      }
    },
    finished:!audioRequired||state==="ended"};
}
