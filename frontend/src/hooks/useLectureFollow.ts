import {useCallback,useLayoutEffect,useRef,useState} from "react";
import type {KeyboardEvent,PointerEvent,WheelEvent,TouchEvent} from "react";
import {BoardMotion} from "../lib/boardMotion.ts";

export function useLectureFollow(conversation:string,enabled:boolean,ready=true){
  const viewport=useRef<HTMLDivElement>(null),content=useRef<HTMLDivElement>(null);
  const motion=useRef<BoardMotion|null>(null),following=useRef(true),active=useRef(enabled);
  active.current=enabled;
  const [readingHistory,setReadingHistory]=useState(false);
  const pointer=useRef<{x:number;y:number}|null>(null);
  const touch=useRef<number|null>(null),returning=useRef(false),followFrame=useRef<number|null>(null);
  const follow=useCallback(()=>{
    const node=viewport.current;
    if(node&&following.current&&active.current)
      // Text must follow each new line without the board's focus dead band.
      motion.current?.follow(node.scrollHeight-node.clientHeight*.45,true);
  },[]);
  const suspend=useCallback(()=>{
    following.current=false;returning.current=false;motion.current?.stop();setReadingHistory(true);
  },[]);
  const resume=useCallback(()=>{
    following.current=true;returning.current=false;setReadingHistory(false);
    const node=viewport.current;
    if(node)motion.current?.follow(node.scrollHeight-node.clientHeight*.45,true);
  },[]);
  const atBottom=()=>{const node=viewport.current;return Boolean(node&&node.scrollHeight-node.scrollTop-node.clientHeight<=4);};
  const scrollIntent=(down:boolean)=>{
    if(!down){suspend();return;}
    if(!following.current){returning.current=true;return;}
    motion.current?.stop();
    if(followFrame.current!==null)cancelAnimationFrame(followFrame.current);
    followFrame.current=requestAnimationFrame(()=>{followFrame.current=null;follow();});
  };
  useLayoutEffect(()=>{
    following.current=true;setReadingHistory(false);
    const node=viewport.current,body=content.current;if(!node||!body)return;
    const reduced=window.matchMedia("(prefers-reduced-motion: reduce)");
    const controller=new BoardMotion(node,{request:cb=>requestAnimationFrame(cb),cancel:id=>cancelAnimationFrame(id)});
    motion.current=controller;controller.setReduced(reduced.matches);
    const preference=()=>controller.setReduced(reduced.matches);
    const observer=new ResizeObserver(follow);observer.observe(body);observer.observe(node);
    reduced.addEventListener("change",preference);follow();
    return()=>{observer.disconnect();controller.stop();motion.current=null;if(followFrame.current!==null)cancelAnimationFrame(followFrame.current);followFrame.current=null;reduced.removeEventListener("change",preference);};
  },[conversation,follow,ready]);
  useLayoutEffect(()=>{if(enabled)follow();else motion.current?.stop();},[enabled,follow]);
  return {viewport,content,readingHistory,resume,
    onScroll:()=>{if(returning.current&&atBottom())resume();},
    onWheel:(event:WheelEvent)=>{if(!event.ctrlKey&&!event.metaKey&&event.deltaY)scrollIntent(event.deltaY>0);},
    onTouchStart:(event:TouchEvent)=>{touch.current=event.touches[0]?.clientY??null;},
    onTouchMove:(event:TouchEvent)=>{
      const y=event.touches[0]?.clientY;
      if(y!==undefined&&touch.current!==null&&Math.abs(y-touch.current)>8){scrollIntent(y<touch.current);touch.current=y;}
    },
    onTouchEnd:()=>{touch.current=null;if(returning.current&&atBottom())resume();},
    onKeyDown:(event:KeyboardEvent)=>{
      if(["PageUp","Home","ArrowUp"].includes(event.key)||event.key===" "&&event.shiftKey)suspend();
      else if(["PageDown","End","ArrowDown"," "].includes(event.key))scrollIntent(true);
    },
    onPointerDown:(event:PointerEvent)=>{
      pointer.current={x:event.clientX,y:event.clientY};
      if(viewport.current&&event.clientX>=viewport.current.getBoundingClientRect().right-18)suspend();
    },
    onPointerMove:(event:PointerEvent)=>{
      if(event.pointerType!=="touch"&&pointer.current&&Math.hypot(event.clientX-pointer.current.x,event.clientY-pointer.current.y)>8)suspend();
    },
    onPointerUp:()=>{pointer.current=null;if(!following.current&&atBottom())resume();}};
}
