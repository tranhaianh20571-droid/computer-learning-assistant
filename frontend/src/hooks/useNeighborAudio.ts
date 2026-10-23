import {useEffect,useRef} from "react";
import {api} from "../api";

/** Warm only published neighbour audio; generation remains owned by the server. */
export function useNeighborAudio(conversation:string,current:number,steps:any[],enabled:boolean){
  const cache=useRef(new Map<string,HTMLAudioElement>());
  const key=JSON.stringify(enabled?steps.filter(step=>step.step_no!==current&&
    Math.abs(step.step_no-current)<=1&&step.audio_ready&&step.audio_id).map(step=>step.audio_id):[]);
  useEffect(()=>{
    const ids:string[]=JSON.parse(key),keep=new Set(ids);
    const release=(audio:HTMLAudioElement)=>{audio.pause();audio.removeAttribute("src");audio.load();};
    for(const [id,audio] of cache.current)if(!keep.has(id)){release(audio);cache.current.delete(id);}
    for(const id of ids)if(!cache.current.has(id)){
      const audio=new Audio();audio.preload="auto";audio.src=api.audioUrl(id);
      cache.current.set(id,audio);audio.load();
    }
    const reconnect=()=>{for(const audio of cache.current.values())audio.load();};
    window.addEventListener("online",reconnect);
    return()=>window.removeEventListener("online",reconnect);
  },[conversation,key]);
  useEffect(()=>()=>{for(const audio of cache.current.values()){
    audio.pause();audio.removeAttribute("src");audio.load();
  }cache.current.clear();},[conversation]);
}
