type AudioBinding={node:MediaElementAudioSourceNode;release:()=>void};
const elements=new Set<HTMLMediaElement>();
const bindings=new WeakMap<HTMLMediaElement,AudioBinding>();
let context:AudioContext|null=null,destination:MediaStreamAudioDestinationNode|null=null;

export function classroomAudio(source?:string){
  const audio=new Audio(source);
  elements.add(audio);connect(audio);
  return audio;
}
function connect(audio:HTMLMediaElement){
  if(!context||bindings.has(audio))return;
  const node=context.createMediaElementSource(audio);
  node.connect(context.destination);
  if(destination)node.connect(destination);
  bindings.set(audio,{node,release:()=>node.disconnect()});
}
export function releaseClassroomAudio(audio:HTMLMediaElement|null){
  if(!audio)return;
  elements.delete(audio);bindings.get(audio)?.release();bindings.delete(audio);
}
export async function classroomAudioStream(){
  context??=new AudioContext();
  await context.resume();
  destination??=context.createMediaStreamDestination();
  for(const audio of elements){
    if(bindings.has(audio))bindings.get(audio)!.node.connect(destination);
    else connect(audio);
  }
  return destination.stream;
}
export function releaseRecordingAudio(){
  if(!destination)return;
  for(const audio of elements){try{bindings.get(audio)?.node.disconnect(destination);}catch{/* Already detached. */}}
  destination=null;
}

const boundaries=new Set<(exiting?:boolean)=>Promise<unknown>>();
export function onClassroomNavigation(callback:(exiting?:boolean)=>Promise<unknown>){
  boundaries.add(callback);return()=>{boundaries.delete(callback);};
}
export async function beforeClassroomNavigation(){
  await Promise.allSettled(Array.from(boundaries,callback=>callback()));
}
export async function beforeClassroomExit(){
  await Promise.allSettled(Array.from(boundaries,callback=>callback(true)));
}

declare global{
  interface Window{
    desktopClassroom?:{
      allowCapture:()=>Promise<void>;
      exportBoard:(payload:unknown,format:"png"|"pdf")=>Promise<{saved:boolean}>;
      readExport:()=>Promise<any>;
      exportReady:()=>Promise<void>;
      onBeforeClose:(callback:()=>Promise<void>)=>()=>void;
    };
  }
}
