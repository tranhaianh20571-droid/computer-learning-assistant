import {useEffect,useRef,useState} from "react";
import {ArrowLeft,Download,RotateCcw} from "lucide-react";
import {recordingApi} from "../api";
import "./lessonRecording.css";

export default function LessonRecordingView({course,lesson,go}:{course:string;lesson:string;go:(path:string)=>void}){
  const [data,setData]=useState<any>(null),[error,setError]=useState(""),[selected,setSelected]=useState("");
  const [busy,setBusy]=useState(""),[reload,setReload]=useState(0);
  const video=useRef<HTMLVideoElement>(null);
  useEffect(()=>{
    let cancelled=false,timer:number|undefined;
    const load=async()=>{
      try{
        const result=await recordingApi.lesson(course,lesson);
        if(!cancelled){
          setData(result);setError("");
          if(result.data?.some((row:any)=>row.status==="recording"))timer=window.setTimeout(()=>void load(),4000);
        }
      }catch{if(!cancelled)setError("录像暂时无法加载，请重试。");}
    };
    void load();
    return()=>{cancelled=true;clearTimeout(timer);};
  },[course,lesson,reload]);
  const rows=data?.data||[],ready=rows.filter((row:any)=>["ready","interrupted"].includes(row.status));
  const recording=ready.find((row:any)=>row.recording_id===selected)||ready[0];
  const pending=rows.filter((row:any)=>row.status==="recording");
  const activeId=recording?.recording_id;
  useEffect(()=>{
    const player=video.current;
    player?.pause();
    return()=>{player?.pause();};
  },[activeId]);
  const recover=async(row:any)=>{
    if(busy)return;setBusy(row.recording_id);
    try{
      if(row.chunks)await recordingApi.finish(row.conversation_id,row.recording_id,{duration_ms:0,complete:false,events:[]});
      else await recordingApi.remove(row.conversation_id,row.recording_id);
      setReload(value=>value+1);
    }catch{setError("已录内容暂时无法恢复。请检查可用空间后重试。");}
    finally{setBusy("");}
  };
  return <div className="lesson-recording">
    <header>
      <button className="iconbtn" aria-label="返回课程" title="返回课程" onClick={()=>{video.current?.pause();go(`/course/${course}`);}}><ArrowLeft size={20}/></button>
      <div><small>课堂录像</small><h1>{data?.title||"复习"}</h1></div>
      {ready.length>1&&<select aria-label="选择录像片段" value={activeId||""} onChange={event=>setSelected(event.target.value)}>
        {ready.map((row:any,index:number)=><option key={row.recording_id} value={row.recording_id}>
          第 {index+1} 段 · {new Date(row.created_at).toLocaleString("zh-CN")}{row.status==="interrupted"?" · 部分录像":""}
        </option>)}
      </select>}
      {recording&&<a href={recordingApi.video(recording.conversation_id,activeId)} download title="下载录像" aria-label="下载录像"><Download size={18}/></a>}
    </header>
    {error&&<p className="recording-notice" role="alert">{error}<button onClick={()=>{setReload(value=>value+1);}}><RotateCcw size={15}/>重试</button></p>}
    {pending.map((row:any)=><div className="recording-notice" role="status" key={row.recording_id}>
      <span>有一段录像尚未保存完成。</span>
      <button disabled={Boolean(busy)} onClick={()=>void recover(row)}><RotateCcw size={15}/>{busy===row.recording_id?"正在恢复…":"恢复已录内容"}</button>
    </div>)}
    <main>
      {recording?<video key={activeId} ref={video} controls playsInline preload="metadata"
        src={recordingApi.video(recording.conversation_id,activeId)} aria-label="课堂录像播放器"
        onError={()=>setError("这段录像无法播放，可重试加载或下载已保存文件。")}/>:
        <div className="recording-empty"><p>{!data&&!error?"正在加载录像…":pending.length?"录像保存完成后即可复习。":"这节课暂无课堂录像。"}</p>
          {data?.conversation_id&&<button onClick={()=>go(`/knowledge/${data.conversation_id}`)}>查看已讲知识点</button>}
        </div>}
    </main>
    {recording&&<footer>
      <span>{recording.status==="interrupted"?"这是中断后恢复的部分录像。":""}</span>
    </footer>}
  </div>;
}
