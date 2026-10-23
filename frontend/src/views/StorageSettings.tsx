import {useCallback, useEffect, useState} from "react";
import {Archive, FolderInput, HardDrive, RefreshCw, Save, Search, Trash2} from "lucide-react";
import {resourceApi} from "../api";
import Dialog from "../ui/Dialog";
import "./storageSettings.css";

type Policy = {automatic_cleanup:boolean;automatic_backup:boolean;prewarm_days:number;prewarm_mib:number;
  prewarm_entries:number;download_days:number;download_mib:number;temporary_hours:number;orphan_days:number;
  audio_warning_mib:number;backup_gib:number};
type Resource = {resource_id:string;kind:string;bytes:number;pinned:boolean;references:number;state:string};
type Job = {job_id:string;kind:string;status:string;progress:number;error_code:string|null};
type Snapshot = {categories:{kind:string;bytes:number;count:number}[];objects:Resource[];jobs:Job[];
  successful_starts:number;observation_only:boolean;audio_warning:boolean;policy:Policy|null;version:number;error_code:string|null;
  directory?:string;backup_directory?:string;old_directories?:string[];
  database_bytes?:number;runtime?:{available:boolean;rss_bytes:number;available_bytes:number;heavy_limit:number;memory_paused:boolean;
    disk?:{free_bytes:number;total_bytes:number;state:string}}};
const names:Record<string,string>={materials:"教材与资料",audio:"课堂音频",characters:"人物素材",prewarm:"预热缓存",
  downloads:"下载缓存",temporary:"临时文件",backup:"备份",unknown:"待核对文件"};
const descriptions:Record<string,string>={
  materials:"保存上传的教材原文件、资料图片和解析结果，供课程规划与教学使用。",
  audio:"保存课堂讲解生成的音频，供课堂播放与历史回放使用。",
  characters:"保存教学人物的立绘、表情等素材，供人物展示和课堂回放使用。",
  prewarm:"保存提前生成的首段教学内容或音频，减少进入课堂后的等待时间。",
  downloads:"保存从链接导入时下载的文件，供资料解析与导入使用。",
  temporary:"保存文件处理过程中产生的临时数据。",
  backup:"保存学习数据的备份副本，用于数据恢复。",
  unknown:"尚未确认用途或归属的文件，需要核对后再处理。",
};
const states:Record<string,string>={queued:"等待执行",running:"正在执行",retry:"等待重试",succeeded:"已完成",failed:"失败"};
const jobs:Record<string,string>={scan:"用量扫描",cleanup:"资源清理",backup:"创建备份",migrate:"目录迁移"};
const diskStates:Record<string,string>={normal:"正常",warning:"空间预警",restricted:"后台任务已暂停",protected:"空间不足"};
function bytes(value=0) {
  if(value>=1024**3)return (value/1024**3).toFixed(2)+" GiB";
  if(value>=1024**2)return (value/1024**2).toFixed(1)+" MiB";
  return (value/1024).toFixed(1)+" KiB";
}
function ResourceDetails({row}:{row:Resource}) {
  return <><p>{descriptions[row.kind]||"本地保存的学习资源文件。"}</p>
    <p>占用 {bytes(row.bytes)} · 当前引用 {row.references} 处</p>
    {row.references>0&&<p>此文件仍被使用，需先删除相关课程、资料或人物后，才能清理。</p>}
    {row.pinned&&<p>此文件已固定保留，当前不能删除。</p>}
  </>;
}

export default function StorageSettings({isAdmin}:{isAdmin:boolean}) {
  const [data,setData]=useState<Snapshot|null>(null),[policy,setPolicy]=useState<Policy|null>(null);
  const [busy,setBusy]=useState(""),[error,setError]=useState(""),[notice,setNotice]=useState("");
  const [destination,setDestination]=useState(""),[migrating,setMigrating]=useState(false);
  const [preview,setPreview]=useState<{resource_ids:string[];bytes:number;count:number}|null>(null);
  const [selected,setSelected]=useState<Resource|null>(null);
  const [removal,setRemoval]=useState<{row:Resource;eligible:boolean}|null>(null);
  const refresh=useCallback(async(signal?:AbortSignal)=>{
    const result=await resourceApi.status(signal) as Snapshot;
    if(!signal?.aborted){setData(result);setPolicy(previous=>previous||result.policy);}
  },[]);
  useEffect(()=>{
    const controller=new AbortController();
    refresh(controller.signal).catch(()=>{if(!controller.signal.aborted)setError("存储信息暂不可用，请重试。");});
    const timer=window.setInterval(()=>refresh(controller.signal).catch(()=>{}),5000);
    return ()=>{controller.abort();window.clearInterval(timer);};
  },[refresh]);
  async function act(name:string,operation:()=>Promise<unknown>,message:string) {
    if(busy)return;
    setBusy(name);setError("");setNotice("");
    try{await operation();await refresh();setNotice(message);}
    catch{setError("操作未完成，请检查权限、空间或作业状态后重试。");}
    finally{setBusy("");}
  }
  if(!data)return <section className="storage-settings"><h2>存储与性能</h2>{error?
    <div role="alert">{error}<button className="btn" onClick={()=>act("refresh",()=>refresh(),"已更新")}>重新加载</button></div>:
    <p role="status">正在读取存储信息…</p>}</section>;
  const runtime=data.runtime;
  const total=data.categories.reduce((sum,row)=>sum+row.bytes,0);
  return <section className="storage-settings" aria-label="存储与性能">
    <header className="storage-heading"><div><h2>存储与性能</h2><p>{bytes(total)} · {data.categories.reduce((sum,row)=>sum+row.count,0)} 个文件</p></div>
      <button className="icon-btn" title="刷新用量" aria-label="刷新用量" disabled={!!busy} onClick={()=>act("refresh",()=>refresh(),"已更新")}><RefreshCw size={18}/></button></header>
    {error&&<p role="alert" className="storage-error">{error}</p>}
    {notice&&<p role="status">{notice}</p>}
    {data.audio_warning&&<p role="status">课堂音频已达到容量提醒阈值，可检查不再需要的课程或迁移存储目录。</p>}
    {data.error_code&&<p role="alert">后台维护需要处理：{data.error_code==="local_resources_migration_required"?"请先升级本地数据库。":"请检查作业失败状态。"}</p>}
    <div className="storage-summary">
      <div><HardDrive size={18}/><span>磁盘可用<strong>{runtime?.disk?bytes(runtime.disk.free_bytes):"仅显示本人用量"}</strong>
        {runtime?.disk&&<small>{diskStates[runtime.disk.state]||"待检查"}</small>}</span></div>
      {runtime&&<div><span>应用进程树内存<strong>{runtime.available?bytes(runtime.rss_bytes):"监测不可用"}</strong>
        <small>{runtime.memory_paused?"重任务已暂停":`重任务并发 ${runtime.heavy_limit}`}</small></span></div>}
      {data.database_bytes!==undefined&&<div><span>本地数据库<strong>{bytes(data.database_bytes)}</strong></span></div>}
    </div>
    <div className="storage-table-wrap"><table><caption>文件用量</caption><thead><tr><th>类别</th><th>数量</th><th>占用</th></tr></thead>
      <tbody>{data.categories.map(row=><tr key={row.kind}><td>{names[row.kind]||row.kind}</td><td>{row.count}</td><td>{bytes(row.bytes)}</td></tr>)}</tbody></table></div>
    {!data.categories.length&&<p>尚未建立用量记录。</p>}
    <div className="storage-actions">
      {isAdmin&&<button className="btn" disabled={!!busy} onClick={()=>act("scan",()=>resourceApi.scan(),"用量扫描已排队")}><Search size={16}/>扫描用量</button>}
      <button className="btn" disabled={!!busy} onClick={()=>act("preview",async()=>setPreview(await resourceApi.preview()),"清理预览已更新")}><Trash2 size={16}/>清理预览</button>
      {isAdmin&&<button className="btn" disabled={!!busy} onClick={()=>act("backup",()=>resourceApi.backup(),"备份已排队")}><Archive size={16}/>创建备份</button>}
    </div>
    {data.observation_only&&<p className="storage-note">正在建立文件记录，自动文件淘汰暂未启用。已完成 {data.successful_starts}/7 次启动对账。</p>}
    {isAdmin&&policy&&<form className="storage-policy" onSubmit={event=>{event.preventDefault();act("policy",
      ()=>resourceApi.savePolicy(policy,data.version),"存储策略已保存");}}>
      <h3>自动维护</h3>
      <label className="storage-check"><input type="checkbox" checked={policy.automatic_cleanup} onChange={event=>setPolicy({...policy,automatic_cleanup:event.target.checked})}/>自动清理可回收文件</label>
      <label className="storage-check"><input type="checkbox" checked={policy.automatic_backup} onChange={event=>setPolicy({...policy,automatic_backup:event.target.checked})}/>空闲时自动备份</label>
      <div className="storage-fields">{([
        ["prewarm_mib","预热缓存上限（MiB）",16,16384],["prewarm_days","预热保留（天）",1,90],
        ["prewarm_entries","预热数量上限",1,1000],["download_mib","下载缓存上限（MiB）",16,32768],
        ["download_days","下载保留（天）",1,90],["temporary_hours","临时文件保留（小时）",1,168],
        ["orphan_days","孤儿文件观察（天）",7,90],["audio_warning_mib","音频提醒阈值（MiB）",128,65536],
        ["backup_gib","自动备份预算（GiB）",1,1024],
      ] as const).map(([key,label,min,max])=><label key={key}>{label}<input type="number" min={min} max={max} required value={policy[key]}
        onChange={event=>setPolicy({...policy,[key]:Number(event.target.value)})}/></label>)}</div>
      <button className="btn" disabled={!!busy}><Save size={16}/>保存策略</button>
    </form>}
    {data.directory&&<div className="storage-location"><h3>存储位置</h3><dl><dt>学习文件</dt><dd>{data.directory}</dd><dt>备份</dt><dd>{data.backup_directory}</dd></dl>
      <button className="btn" disabled={!!busy} onClick={()=>setMigrating(true)}><FolderInput size={16}/>迁移学习文件</button>
      {!!data.old_directories?.length&&<><h4>保留的旧副本</h4>{data.old_directories.map(path=><p key={path}>{path}</p>)}</>}
    </div>}
    <div className="storage-table-wrap"><table><caption>文件缓存</caption><thead><tr><th>类别</th><th>占用</th><th>引用</th><th>操作</th></tr></thead>
      <tbody>{data.objects.map(row=><tr className="storage-resource-row" key={row.resource_id} onClick={()=>setSelected(row)}>
        <td><button className="storage-resource-info" aria-label={`查看${names[row.kind]||row.kind} ${row.resource_id}`}
          onClick={event=>{event.stopPropagation();setSelected(row);}}>{names[row.kind]||row.kind}<small>{row.resource_id.slice(-8)}</small></button></td>
        <td>{bytes(row.bytes)}</td><td>{row.references}</td><td><button className="storage-delete" aria-label={"删除 "+row.resource_id}
          title="删除此缓存" disabled={!!busy} onClick={event=>{event.stopPropagation();act("delete-preview",async()=>{
            const result=await resourceApi.preview();
            setRemoval({row,eligible:result.resource_ids.includes(row.resource_id)});
          },"");}}><Trash2 size={17}/></button></td></tr>)}</tbody></table></div>
    {selected&&<Dialog title={`${names[selected.kind]||selected.kind}介绍`} kind="info" onClose={()=>setSelected(null)}>
      <ResourceDetails row={selected}/>
    </Dialog>}
    {removal&&<Dialog title="删除缓存" kind="info" onClose={()=>setRemoval(null)} actions={<>
      <button className="btn" onClick={()=>setRemoval(null)}>{removal.eligible?"取消":"关闭"}</button>
      {removal.eligible&&<button className="btn" disabled={!!busy} onClick={()=>act("delete",async()=>{
        await resourceApi.cleanup([removal.row.resource_id]);setRemoval(null);
      },"此缓存的删除已排队")}><Trash2 size={16}/>确认删除</button>}
    </>}>
      <ResourceDetails row={removal.row}/>
      <p>{removal.eligible?"删除后将释放此文件占用的空间，此操作无法撤销。":
        removal.row.references>0||removal.row.pinned?"此文件暂不能删除。":"此文件尚未达到清理条件，或其用途、归属仍需核对，暂不能删除。"}</p>
    </Dialog>}
    {!!data.jobs.length&&<div className="storage-jobs"><h3>维护作业</h3>{data.jobs.map(job=><div key={job.job_id}>
      <span>{jobs[job.kind]||job.kind}</span><span>{states[job.status]||job.status} · {job.progress} 项</span>
      {job.error_code&&<small role="status">{job.error_code}</small>}
      {["failed","retry"].includes(job.status)&&<button className="btn" disabled={!!busy}
        onClick={()=>act("retry",()=>resourceApi.retry(job.job_id),"作业已重新排队")}><RefreshCw size={15}/>重试</button>}</div>)}</div>}
    {preview&&<Dialog title="清理预览" kind="info" onClose={()=>setPreview(null)} actions={<>
      <button className="btn" onClick={()=>setPreview(null)}>取消</button><button className="btn" disabled={!!busy||!preview.count}
        onClick={()=>act("cleanup",async()=>{await resourceApi.cleanup(preview.resource_ids);setPreview(null);},"清理已排队")}><Trash2 size={16}/>确认清理</button></>}>
      <p>预计可回收 {preview.count} 个文件，共 {bytes(preview.bytes)}。</p><p>固定文件、仍在使用的文件和有效教材不会被清理。</p>
    </Dialog>}
    {migrating&&<Dialog title="迁移学习文件" kind="info" onClose={()=>setMigrating(false)} actions={<>
      <button className="btn" onClick={()=>setMigrating(false)}>取消</button><button className="btn" disabled={!!busy||!destination.trim()}
        onClick={()=>act("migrate",async()=>{await resourceApi.migrate(destination);setMigrating(false);},"迁移已排队")}><FolderInput size={16}/>开始迁移</button></>}>
      <label className="storage-destination">新目录<input value={destination} onChange={event=>setDestination(event.target.value)} placeholder="选择一个空目录的完整路径"/></label>
      <p>校验完成后切换存储位置，原目录保留为旧副本。</p>
    </Dialog>}
  </section>;
}
