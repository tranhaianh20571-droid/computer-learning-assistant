import {useEffect,useRef,useState} from "react";
import {api} from "../api";
import {audioCharacters} from "../lib/audioTiming";
import {classroomAudio,releaseClassroomAudio} from "../lib/classroomMedia";

export function useFeynmanAudio(conversation:string,quiz:any,autoplay:boolean,onRefresh:()=>Promise<void>){
  const latest=useRef({quiz,onRefresh});latest.current={quiz,onRefresh};
  const turn=quiz?.history?.filter((item:any)=>item.role==="assistant").at(-1);
  const media=useRef<HTMLAudioElement|null>(null),sequence=useRef(0);
  const intent=useRef(autoplay);
  const [cursor,setCursor]=useState({id:"",chars:0,done:false,state:"paused"});
  useEffect(()=>{
    if(!turn)return;
    const current=turn,required=quiz.audio_required;
    sequence.current=Math.max(Date.now(),current.playback?.client_seq||0);
    let disposed=false,frame=0,lastSave=0,stalled=false;
    const done=Boolean(current.playback?.ended);
    let visible=done?Array.from(current.text).length:0;
    setCursor({id:current.id,chars:required?visible:Array.from(current.text).length,done,state:done?"ended":"paused"});
    const save=async(ended=false)=>{
      const position=Math.floor((media.current?.currentTime||0)*1000);
      try{
        await api.feynmanAction(conversation,"playback",{turn_id:current.id,client_seq:++sequence.current,
          position_ms:position,ended});
        if(ended&&!disposed){setCursor({id:current.id,chars:Array.from(current.text).length,done:true,state:"ended"});await latest.current.onRefresh();}
      }catch{if(ended&&!disposed)setCursor(old=>({...old,state:"failed"}));}
    };
    if(!required){void save(true);return()=>{disposed=true;};}
    if(!current.audio_id)return()=>{disposed=true;};
    const audio=classroomAudio(api.audioUrl(current.audio_id));audio.preload="auto";media.current=audio;
    const update=()=>{
      if(!stalled&&!audio.paused&&!audio.seeking&&audio.readyState>=2){
        visible=audioCharacters(current.text,current.subtitles||[],Math.floor(audio.currentTime*1000));
        setCursor(old=>({...old,id:current.id,chars:Math.max(old.chars,visible),state:"playing"}));
        if(Date.now()-lastSave>1500){lastSave=Date.now();void save();}
      }
      frame=requestAnimationFrame(update);
    };
    frame=requestAnimationFrame(update);
    audio.onloadedmetadata=()=>{if(current.playback?.position_ms&&!done)audio.currentTime=current.playback.position_ms/1000;};
    audio.onplaying=()=>{if(disposed||!intent.current){audio.pause();return;}stalled=false;setCursor(old=>({...old,state:"playing"}));};
    audio.onwaiting=()=>{stalled=true;setCursor(old=>({...old,state:"waiting"}));};
    audio.onseeking=()=>{stalled=true;setCursor(old=>({...old,state:"waiting"}));};
    audio.onseeked=()=>{if(!audio.paused&&audio.readyState>=2){stalled=false;setCursor(old=>({...old,state:"playing"}));}};
    audio.onerror=()=>{stalled=true;setCursor(old=>({...old,state:"failed"}));};
    audio.onended=()=>{if(!disposed&&intent.current)void save(true);};
    if(autoplay&&!done)void audio.play().catch(()=>{if(!disposed)setCursor(old=>({...old,state:"blocked"}));});
    const timer=setTimeout(()=>{if(!disposed&&autoplay&&!done&&audio.paused)setCursor(old=>({...old,state:"blocked"}));},12000);
    return()=>{
      disposed=true;clearTimeout(timer);cancelAnimationFrame(frame);
      void save();audio.onplaying=null;audio.onwaiting=null;audio.onseeking=null;audio.onseeked=null;
      audio.onerror=null;audio.onended=null;audio.onloadedmetadata=null;
      audio.pause();audio.removeAttribute("src");audio.load();releaseClassroomAudio(audio);media.current=null;
    };
  },[conversation,turn?.id,turn?.audio_id]);
  useEffect(()=>{
    intent.current=autoplay;
    if(!autoplay){media.current?.pause();setCursor(old=>old.state==="playing"?{...old,state:"paused"}:old);}
  },[autoplay]);
  const play=()=>{
    const audio=media.current;if(!audio)return;
    if(!audio.paused){intent.current=false;audio.pause();setCursor(old=>({...old,state:"paused"}));return;}
    if(audio.error)audio.load();
    intent.current=true;void audio.play().catch(()=>{
      if(media.current===audio&&intent.current)setCursor(old=>({...old,state:"blocked"}));
    });
  };
  return {turn,done:Boolean(turn&&((cursor.id===turn.id&&cursor.done)||turn.playback?.ended)),
    state:cursor.state,play,pause:()=>{intent.current=false;media.current?.pause();setCursor(old=>({...old,state:"paused"}));},
    text:(item:any)=>item.role!=="assistant"||!quiz?.audio_required||item.playback?.ended?item.text
      :item.id===cursor.id?Array.from(item.text as string).slice(0,cursor.chars).join(""):""};
}
