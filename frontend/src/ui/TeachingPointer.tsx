import {useLayoutEffect,useRef,useState} from "react";
import {fitPointer} from "../lib/pointingRig";

export default function TeachingPointer({target,active}:{target:{x:number;y:number;visible:boolean}|null;active:boolean}){
  const host=useRef<HTMLDivElement>(null),[point,setPoint]=useState<ReturnType<typeof fitPointer>>(null);
  useLayoutEffect(()=>{
    const node=host.current;if(!node||!active||!target?.visible){setPoint(null);return;}
    const update=()=>{
      const rect=node.getBoundingClientRect();
      const next=fitPointer(target.x-rect.left,target.y-rect.top,rect.width,rect.height);
      setPoint(old=>JSON.stringify(old)===JSON.stringify(next)?old:next);
    };
    update();const observer=new ResizeObserver(update);observer.observe(node);
    return()=>observer.disconnect();
  },[target?.x,target?.y,target?.visible,active]);
  return <div ref={host} className="teaching-pointer-layer" aria-hidden="true">
    {point&&point.length>24&&<span className="teaching-pointer" style={{left:point.x,top:point.y,width:point.length,transform:`rotate(${point.angle}deg)`}}><i/><b/></span>}
  </div>;
}
