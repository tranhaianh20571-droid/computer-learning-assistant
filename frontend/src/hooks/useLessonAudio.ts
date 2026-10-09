import {useEffect,useRef,useState} from "react";
import {api} from "../api";
import {audioCharacters} from "../lib/audioTiming.ts";

export function lessonAudioRequired(workspace:any,step:any,current:number){
  if(!step?.text || workspace?.tts_authorized===false || workspace?.audio_mode==="text")return false;
  const pending=workspace?.queued_tasks?.some((task:any)=>task.kind==="tts"&&task.step_no===current&&["queued","leased","running"].includes(task.status));
  return Boolean(workspace?.tts_authorized===true || workspace?.audio_mode==="tts" || (step.audio_ready&&step.audio_id) || pending);
}

export const LESSON_AUDIO_WAIT_MS = 12_000;

export function useLessonAudio(conversation:string,current:number,step:any,playing:boolean,bufferRef:any,posRef:any,resume:any,audioRequired=false){
  const media=useRef<HTMLAudioElement|null>(null),source=useRef(""),currentStep=useRef(step);
  const [state,setState]=useState<"idle"|"playing"|"paused"|"blocked"|"ended"|"failed">("idle");
  const [textOnly,setTextOnly]=useState(false);
  const latestTextOnly=useRef(textOnly);latestTextOnly.current=textOnly;
  const latestAudioRequired=useRef(audioRequired);latestAudioRequired.current=audioRequired;
  currentStep.current=step;
  const latestPlaying=useRef(playing);latestPlaying.current=playing;
  const audioWait=useRef<{key:string;deadline:number}|null>(null);
  const useText=()=>{
    latestTextOnly.current=true;
    media.current?.pause();
    setState("paused");setTextOnly(true);
    bufferRef.current.audioGate(`step:${current}`,false);
    bufferRef.current.audioClock(`step:${current}`,null);
  };
  const play=()=>{
    if(latestTextOnly.current||!latestAudioRequired.current)return;
    const audio=media.current;if(!audio||!source.current)return;
    const expected=source.current;
    audio.play().then(()=>{if(source.current===expected&&!latestTextOnly.current&&latestAudioRequired.current){const item=currentStep.current;const chars=audioCharacters(item?.text||"",item?.audio_subtitles||[],posRef.current);bufferRef.current.audioGate(`step:${item?.step_no}`,false);bufferRef.current.audioClock(`step:${item?.step_no}`,chars);setState("playing");}else if(latestTextOnly.current||!latestAudioRequired.current)audio.pause();}).catch(error=>{
      if(source.current===expected&&!latestTextOnly.current)setState(error.name==="NotAllowedError"?"blocked":"failed");
    });
  };
  useEffect(()=>{
    const audio=new Audio();audio.preload="auto";media.current=audio;
    let frame=0;
    const tick=()=>{
      if(source.current&&latestPlaying.current&&!audio.paused){
        posRef.current=Math.floor(audio.currentTime*1000);
        bufferRef.current.audioClock(`step:${currentStep.current?.step_no}`,audioCharacters(currentStep.current?.text||"",
          currentStep.current?.audio_subtitles||[],posRef.current));
      }
      frame=requestAnimationFrame(tick);
    };
    frame=requestAnimationFrame(tick);
    audio.onended=()=>{setState("ended");bufferRef.current.audioClock(`step:${currentStep.current?.step_no}`,
      Array.from(currentStep.current?.text||"").length);};
    audio.onerror=()=>{if(source.current)setState("failed");};
    return()=>{cancelAnimationFrame(frame);audio.pause();audio.removeAttribute("src");audio.load();media.current=null;source.current="";};
  },[conversation]);
  useEffect(()=>{
    setTextOnly(false);
  },[conversation,current]);
  useEffect(()=>{
    const audio=media.current;if(!audio)return;
    audio.pause();setState("idle");source.current="";
    audio.removeAttribute("src");audio.load();
    if(audioRequired&&step?.audio_ready&&step.audio_id){
      source.current=step.audio_id;audio.src=api.audioUrl(step.audio_id);
      const offset=resume?.step_no===current&&resume?.presentation?.audio_id===step.audio_id?resume.position_ms:0;
      posRef.current=offset;
      let seeked=offset<=0;
      const restorePosition=()=>{
        if(seeked)return;
        try{
          audio.currentTime=Number.isFinite(audio.duration)?Math.min(offset/1000,Math.max(0,audio.duration-.05)):offset/1000;
          seeked=true;
        }catch{/* The canplay event retries after the source becomes seekable. */}
      };
      audio.onloadedmetadata=restorePosition;audio.oncanplay=restorePosition;
      if(latestPlaying.current&&!latestTextOnly.current)play();else setState("paused");
    }
    return()=>{audio.pause();audio.onloadedmetadata=null;audio.oncanplay=null;bufferRef.current.audioClock(`step:${current}`,null);};
  },[conversation,current,step?.audio_id,step?.audio_ready,audioRequired]);
  useEffect(()=>{
    if(!media.current||!source.current)return;
    if(!playing){media.current.pause();if(state!=="ended")setState("paused");}
    else if(!textOnly&&state!=="ended")play();
  },[playing,textOnly]);
  useEffect(()=>{
    if(audioRequired&&!latestTextOnly.current&&state!=="playing"&&state!=="ended"){
      bufferRef.current.audioGate(`step:${current}`,true);
    }else if(!audioRequired||latestTextOnly.current)bufferRef.current.audioClock(`step:${current}`,null);
  },[state,current,audioRequired]);
  useEffect(()=>{
    const key=`step:${current}`;
    if(audioRequired&&!textOnly)bufferRef.current.audioGate(key,true);
    else bufferRef.current.audioGate(key,false);
  },[current,audioRequired,textOnly]);
  useEffect(()=>{
    if(!step?.text||!audioRequired||textOnly||!playing||state==="playing"||state==="ended"){audioWait.current=null;return;}
    if(state==="failed"){useText();return;}
    const key=`${conversation}:${current}`;
    if(audioWait.current?.key!==key)audioWait.current={key,deadline:Date.now()+LESSON_AUDIO_WAIT_MS};
    const remaining=Math.max(0,audioWait.current.deadline-Date.now());
    const timer=window.setTimeout(useText,remaining);
    return()=>window.clearTimeout(timer);
  },[conversation,current,Boolean(step?.text),audioRequired,textOnly,playing,state]);
  return {state,textOnly:textOnly||!audioRequired,sourceId:!textOnly&&audioRequired?source.current:null,pause:()=>{
      if(media.current&&source.current&&!latestTextOnly.current)posRef.current=Math.floor(media.current.currentTime*1000);
      media.current?.pause();
    },enable:play,
    skip:useText,
    finished:!audioRequired||textOnly||state==="ended"};
}
