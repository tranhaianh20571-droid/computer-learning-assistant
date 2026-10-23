import {useCallback,useRef} from "react";

/** One explicit pause boundary; incoming work never grants playback permission. */
export function useClassroomPresentation(){
  const epoch=useRef(0);
  const stop=useCallback((buffer:any,...channels:(()=>void)[])=>{
    epoch.current++;
    buffer?.pause(true);
    channels.forEach(pause=>pause());
  },[]);
  return {epoch,stop};
}
