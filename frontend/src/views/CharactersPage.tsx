import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, X, Play, Upload, WandSparkles, Bookmark, Trash2, ArrowLeft, Download, UploadCloud } from "lucide-react";
import { api, characterApi } from "../api";
import { errText } from "../lib/errors";
import "../characters.css";
import CharacterCreation from "../ui/CharacterCreation";
import PersonaEditor from "../ui/PersonaEditor";
import MarketUpload from "../ui/MarketUpload";
import { useMarketImportPoller } from "../hooks/useMarketImportPoller";
import "../market.css";

export type CharacterProfile = { name: string; identity: string; background: string; personality: string; appearance: string; speaking_style?:string; tags: string[]; voice_id: string; voice_config_id: string; voice_selection?:"auto"|"manual"; media_ids: string[]; base_asset_id: string; pointing_rig?:any };
export type CharacterCard = { card_id: string; revision: number; voice_choice_version?:number; system_default?:boolean; can_publish?:boolean; quality_summary?:{grade?:string;default_eligible?:boolean;warnings?:string[]}|null; published_revision: number | null; editable: boolean; collected: boolean; profile: CharacterProfile; assets: { asset_id: string; kind: string; scene: string; image_part?:string; confirmed: boolean; url: string }[] };
const empty: CharacterProfile = { name: "", identity: "", background: "", personality: "", appearance: "", tags: [], voice_id: "", voice_config_id: "", media_ids: [], base_asset_id: "" };
const scenes = [{ id: "neutral", name: "自然站姿" }, { id: "explaining", name: "讲解" }, { id: "thinking", name: "思考" }, { id: "encouraging", name: "鼓励" }, { id: "celebrating", name: "庆祝" }];

export default function CharactersPage({ market = false, cardId, go, onErr,conversationId }: { market?: boolean; cardId?: string;conversationId?:string; go: (path: string) => void; onErr: (message: string) => void }) {
  const [preference,setPreference]=useState<any>(null),[preferenceError,setPreferenceError]=useState(""),[useMessage,setUseMessage]=useState("");
  const loadPreference=useCallback(()=>characterApi.preference(conversationId).then(value=>{setPreference(value);setPreferenceError("");}).catch(()=>setPreferenceError("导师偏好读取失败，请重试。")),[conversationId]);
  useEffect(()=>{void loadPreference();},[loadPreference]);
  const cardPath=(id:string)=>`/card/${id}${conversationId?`?conversation=${encodeURIComponent(conversationId)}`:""}`;
  const libraryPath=`/cards${conversationId?`?conversation=${encodeURIComponent(conversationId)}`:""}`;
  const [cards, setCards] = useState<CharacterCard[]>([]);
  const [selected, setSelected] = useState<CharacterCard | null>(null);
  const [profile, setProfile] = useState<CharacterProfile>({ ...empty });
  const [choosingMethod,setChoosingMethod]=useState(false);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [creating, setCreating] = useState(false), [configs, setConfigs] = useState<any[]>([]);
  const [voices, setVoices] = useState<any[]>([]), [scene, setScene] = useState("neutral"), [configId, setConfigId] = useState("");
  const [jobs, setJobs] = useState<any[]>([]);
  const [recommended, setRecommended] = useState("");
  const [voiceLoading,setVoiceLoading]=useState(false),[voiceError,setVoiceError]=useState("");
  const [voiceMatch,setVoiceMatch]=useState("");
  const [duration, setDuration] = useState(5);
  const uploadRef = useRef<HTMLInputElement>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const [importing, setImporting] = useState(false);
  const [marketOpen, setMarketOpen] = useState(false);
  const live = useRef(true), lock = useRef(false);
  const listRequest = useRef<AbortController | null>(null);
  const configRequest = useRef<AbortController | null>(null);
  const voiceRequest = useRef<AbortController | null>(null);
  const previewRequest=useRef<AbortController|null>(null),previewAudio=useRef<HTMLAudioElement|null>(null);
  const [previewUrl,setPreviewUrl]=useState("");
  useEffect(()=>()=>{previewRequest.current?.abort();previewAudio.current?.pause();},[]);
  useEffect(()=>()=>{if(previewUrl)URL.revokeObjectURL(previewUrl);},[previewUrl]);
  useEffect(()=>{previewRequest.current?.abort();previewAudio.current?.pause();setPreviewUrl("");},[cardId,profile.voice_id]);
  const [configError, setConfigError] = useState(""), [configLoading, setConfigLoading] = useState(false);
  useEffect(() => { live.current = true; return () => {
    live.current = false; listRequest.current?.abort(); configRequest.current?.abort(); voiceRequest.current?.abort();
  }; }, []);
  const refresh = useCallback(async () => {
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setLoading(true); setError("");
    try {
      const list = await characterApi.list(market, controller.signal);
      if (!live.current || controller.signal.aborted) return;
      setCards(list.data);
      if (cardId) {
        const card = await characterApi.get(cardId, controller.signal);
        if (!live.current || controller.signal.aborted) return;
        setSelected(card); setProfile(card.profile);
        const nextJobs = card.editable ? (await characterApi.jobs(cardId, controller.signal)).data : [];
        if (live.current && !controller.signal.aborted) setJobs(nextJobs);
      }
    } catch (failure) { if (live.current && !controller.signal.aborted) setError(errText(failure)); }
    finally { if (live.current && !controller.signal.aborted) setLoading(false); }
  }, [market, cardId]);
  useEffect(() => { setSelected(null); setCreating(false); setVoices([]); void refresh(); }, [refresh]);
  const needsConfigs = Boolean(creating || selected);
  const refreshConfigs = useCallback(async () => {
    configRequest.current?.abort();
    const controller = new AbortController();
    configRequest.current = controller;
    setConfigLoading(true); setConfigError("");
    try {
      const result = await api.listConfigs(undefined, controller.signal);
      if (live.current && !controller.signal.aborted) setConfigs(result.data.filter((item: any) => item.is_active));
    } catch {
      if (live.current && !controller.signal.aborted) setConfigError("加载人物服务配置失败，请重试。");
    } finally {
      if (live.current && !controller.signal.aborted) setConfigLoading(false);
    }
  }, []);
  useEffect(() => {
    if (needsConfigs) void refreshConfigs();
    return () => configRequest.current?.abort();
  }, [needsConfigs, refreshConfigs]);
  useEffect(() => {
    if (!selected || !jobs.some(job => ["queued", "leased", "running"].includes(job.status))) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await characterApi.jobs(selected.card_id);
        if (!live.current) return;
        if(next.data.some((job:any)=>["failed","expired"].includes(job.status)&&
          jobs.some(previous=>previous.task_id===job.task_id&&["queued","leased","running"].includes(previous.status)))){
          setError("图片生成未完成，已保留原有图片，可以重新生成。");
        }
        setJobs(next.data);
        const card = await characterApi.get(selected.card_id);
        if (live.current) {
          setSelected(card);
          if(card.revision!==selected.revision)setProfile(card.profile);
        }
      } catch { /* A user retry still reads durable job status. */ }
    }, 2000);
    return () => clearInterval(timer);
  }, [selected?.card_id, selected?.revision, jobs]);
  const action = async (run: () => Promise<unknown>) => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError("");
    try { await run(); } catch (failure) { if (live.current) { setError(errText(failure)); onErr(errText(failure)); } }
    finally { lock.current = false; if (live.current) setBusy(false); }
  };
  useMarketImportPoller(() => { void refresh(); });
  const importCards = (files: FileList) => action(async () => {
    setImporting(true);
    try {
      for (const file of Array.from(files)) await characterApi.importCard(file);
      await refresh();
    } finally {
      setImporting(false);
    }
  });
  const field = (key: keyof CharacterProfile, value: any) => setProfile(old => ({ ...old, [key]: value }));
  const voiceConfig=configs.find(item=>item.kind==="tts"&&item.config_id===profile.voice_config_id)
    ||configs.find(item=>item.kind==="tts"&&item.protocol==="minimax");
  const expressionInput=JSON.stringify({name:profile.name||"新人物",identity:profile.identity,background:profile.background,
    personality:profile.personality,appearance:profile.appearance,tags:profile.tags,voice_id:"",voice_config_id:""});
  const loadVoices=useCallback(async(signal?:AbortSignal)=>{
    voiceRequest.current?.abort();
    const controller=new AbortController();
    voiceRequest.current=controller;
    const requestSignal=signal?AbortSignal.any([signal,controller.signal]):controller.signal;
    setVoiceLoading(true);setVoiceError("");
    try{
      const result=await characterApi.expression(JSON.parse(expressionInput),voiceConfig?.config_id||"",requestSignal);
      if(!requestSignal.aborted&&live.current){
        if(result.match_status==="catalog_unavailable"){
          setVoiceError("声音目录暂时不可用，已保留当前音色，请重试。");
          setProfile(old=>({...old,speaking_style:result.speaking_style}));
          return;
        }
        setVoices(result.data);setRecommended(result.recommended_voice_id||"");setVoiceMatch(result.match_status);
        setProfile(old=>({...old,speaking_style:result.speaking_style,
          ...(old.voice_selection==="manual"&&result.data.some((voice:any)=>voice.voice_id===old.voice_id)?{}:
            (result.compatible_voice_ids||result.data.map((voice:any)=>voice.voice_id)).includes(old.voice_id)?{}:
            result.recommended_voice_id?{voice_id:result.recommended_voice_id,voice_config_id:voiceConfig.config_id}:
            voiceConfig&&result.voice_gender!=="unknown"&&old.voice_id?{voice_id:"",voice_config_id:""}:{})}));
      }
    }
    catch{if(!requestSignal.aborted&&live.current)setVoiceError("人物声音匹配失败，已保留当前音色和风格，请重试。");}
    finally{if(!requestSignal.aborted&&live.current)setVoiceLoading(false);}
  },[expressionInput,voiceConfig?.config_id,voiceConfig?.config_version]);
  useEffect(()=>{
    if(!needsConfigs||configLoading||configError)return;
    const controller=new AbortController();
    const timer=window.setTimeout(()=>void loadVoices(controller.signal),350);
    return()=>{clearTimeout(timer);controller.abort();};
  },[needsConfigs,configLoading,configError,loadVoices]);
  const selectMentor=async(card:CharacterCard|null)=>{
    if(!preference)return;
    let result;
    try{result=await characterApi.use({expected_version:preference.version,card_id:card?.card_id||null,
      revision:card?.revision||null,conversation_id:conversationId||null});}
    catch(error){await loadPreference();throw error;}
    setPreference(result);await loadPreference();setUseMessage(result.effective_step_no?`当前课堂第 ${result.effective_step_no} 段起重新准备，已讲完的内容保留。`:"之后新课堂使用此选择。");
  };
  const using=(card:CharacterCard|null,voiceId?:string)=>{
    if(!card||!preference)return false;
    const current=conversationId?preference.classroom:preference;
    return current?.card_id===card.card_id&&current?.revision===card.revision
      &&(!conversationId||!voiceId||current.voice_id===voiceId);
  };
  const chooseVoice=async(voiceId:string)=>{
    const next={...profile,voice_id:voiceId,voice_config_id:voiceConfig.config_id,voice_selection:"manual" as const};
    setProfile(next);setPreviewUrl("");
    if(selected){
      const updated=await characterApi.chooseVoice(selected.card_id,{revision:selected.revision,
        expected_version:selected.voice_choice_version||0,voice_id:voiceId,config_id:voiceConfig.config_id});
      setSelected(updated);setProfile({...next,voice_id:updated.profile.voice_id,
        voice_config_id:updated.profile.voice_config_id,voice_selection:updated.profile.voice_selection});
      if(preference?.card_id===selected.card_id)await selectMentor(updated);
    }
  };
  const previewVoice=async()=>{
    previewRequest.current?.abort();const controller=new AbortController();previewRequest.current=controller;
    const timeout=window.setTimeout(()=>controller.abort(),65_000);
    try{
      const blob=await characterApi.previewVoice({...profile,name:profile.name||"新人物"},voiceConfig.config_id,controller.signal);
      if(live.current&&!controller.signal.aborted)setPreviewUrl(URL.createObjectURL(blob));
    }finally{clearTimeout(timeout);if(previewRequest.current===controller)previewRequest.current=null;}
  };
  const save = async (rebind=true) => {
    const data=profile.voice_id&&voiceConfig?{...profile,voice_config_id:voiceConfig.config_id}:profile;
    if(selected&&Object.keys({...selected.profile,...data}).filter(key=>key!=="speaking_style").every(
      key=>JSON.stringify(data[key as keyof CharacterProfile])===JSON.stringify(selected.profile[key as keyof CharacterProfile])))return selected;
    const card = selected ? await characterApi.edit(selected.card_id, selected.revision, data) : await characterApi.create(data);
    if(!selected)go(cardPath(card.card_id));
    setSelected(card); setProfile(card.profile); setCreating(false);
    if(rebind&&preference?.card_id===card.card_id)await selectMentor(card);
    return card as CharacterCard;
  };
  const editor = creating || selected;
  const editable = creating || selected?.editable;
  const mediaPending=jobs.some(job=>["queued","leased","running"].includes(job.status));
  const currentSceneAssets=selected?.assets.filter(asset=>asset.kind!=="voice"&&asset.scene===scene&&
    (!asset.image_part||asset.image_part==="portrait")&&profile.media_ids.includes(asset.asset_id));
  const sceneAsset=scene==="neutral"?currentSceneAssets?.find(asset=>asset.asset_id===profile.base_asset_id):
    currentSceneAssets?.at(-1);
  const uploadMedia=async(file:File)=>{
    const saved=await save();
    const result=await characterApi.upload(saved.card_id,saved.revision,file,scene);
    await characterApi.confirm(saved.card_id,result.asset_id);
    const current=await characterApi.get(saved.card_id) as CharacterCard;
    const retained=current.profile.media_ids.filter(id=>!current.assets.some(asset=>
      asset.asset_id===id&&asset.scene===scene&&(!asset.image_part||asset.image_part==="portrait")));
    const next={...current.profile,media_ids:[...retained,result.asset_id],
      ...(file.type.startsWith("image/")&&scene==="neutral"?{base_asset_id:result.asset_id,pointing_rig:null}:{})};
    const updated=await characterApi.edit(current.card_id,current.revision,next);
    setSelected(updated);setProfile(updated.profile);
  };
  const base = selected?.assets.find(asset => asset.asset_id === profile.base_asset_id && asset.kind === "image");
  if(choosingMethod)return <CharacterCreation onClose={()=>setChoosingMethod(false)}
    onCustom={()=>{setChoosingMethod(false);setProfile({...empty});setSelected(null);setCreating(true);}}
    onAccepted={card=>{setChoosingMethod(false);setSelected(card);setProfile(card.profile);setCreating(false);go(cardPath(card.card_id));}}/>;
  return <div className="character-page">
    <header className="character-header"><div><h1>{editor ? profile.name || "新人物" : market ? "卡片市集" : "我的人物"}</h1>
      {editor && <span>{selected ? `版本 ${selected.revision}` : "草稿"}</span>}</div>
      <div className="row">{conversationId&&<button className="btn" onClick={()=>go(`/lecture/${conversationId}`)}>返回课堂</button>}{editor ? <button className="iconbtn" aria-label="返回人物列表" title="返回人物列表" onClick={() => go(market ? "/card-market" : libraryPath)}><ArrowLeft size={20} /></button> : <>
        {market && <a className="btn" href="#/cards">我的人物</a>}
        {!market && <>
          <button className="btn" disabled={importing||busy} onClick={() => importRef.current?.click()}><Upload size={16}/>{importing?"导入中…":"导入卡片"}</button>
          <button className="btn" disabled={busy} onClick={() => setMarketOpen(true)}><UploadCloud size={16}/>上传到市集</button>
          <input ref={importRef} hidden type="file" multiple accept=".zip,application/zip" onChange={e => { const files=e.target.files; e.target.value=""; if(files?.length) void importCards(files); }} />
          <button className="btn btn-pri" onClick={() => setChoosingMethod(true)}><Plus size={16} />新人物</button>
        </>}</>}</div>
    </header>
    {preferenceError&&<p role="alert">{preferenceError}<button className="btn" onClick={()=>{setPreferenceError("");void loadPreference();}}>重试</button></p>}
    {useMessage&&<p role="status">{useMessage}</p>}
    {error && <div role="alert" className="notice">{error}<button className="btn btn-sm" onClick={refresh}>重新加载</button></div>}
    {loading ? <p role="status">正在加载人物…</p> : !editor ? <div className="character-library">
      {cards.length === 0 && <div className="character-empty"><p>{market ? "市集还没有已发布的人物。" : "还没有人物卡。"}</p>{!market && <button className="btn" onClick={() => setChoosingMethod(true)}>创建人物</button>}</div>}
      {cards.map(card => {
        const image = card.assets.find(asset => asset.asset_id === card.profile.base_asset_id);
        return <article className="character-tile" key={card.card_id}>
          <button className="character-cover" aria-label={`查看 ${card.profile.name}`} onClick={() => go(cardPath(card.card_id))}>{image ? <img src={characterApi.assetUrl(image.url)} alt={card.profile.name} /> : <span>{card.profile.name.slice(0, 1)}</span>}</button>
          <div className="character-tile-meta"><h2><a href={`#${cardPath(card.card_id)}`}>{card.profile.name}</a></h2><p>{card.profile.identity}</p>
            {card.quality_summary?.grade&&<p>{({limited:"资料有限 · 人物可用",grounded:"资料较充分",authored:"原创角色",draft:"待修订"} as Record<string,string>)[card.quality_summary.grade]||""}</p>}
            <div className="character-tags">{card.profile.tags.map(tag => <span key={tag}>{tag}</span>)}</div></div>
          {market && <button className="iconbtn" aria-label={card.system_default ? "系统默认导师" : card.collected ? "取消收录" : "收录人物"} title={card.system_default ? "系统默认导师" : card.collected ? "取消收录" : "收录人物"} disabled={busy||card.system_default} onClick={() => action(async () => { await characterApi.collect(card.card_id, !card.collected); await refresh(); })}><Bookmark size={18} fill={card.collected ? "currentColor" : "none"} /></button>}
          {!market&&<button className="btn" disabled={busy||!preference||using(card)} onClick={()=>action(()=>selectMentor(card))}>{using(card)?"正在使用":"使用导师"}</button>}
          {!market&&<a className="iconbtn" href={characterApi.exportUrl(card.card_id)} download title="导出卡片包" aria-label={`导出 ${card.profile.name}`}><Download size={18}/></a>}
        </article>;
      })}
    </div> : <div className="character-editor">
      <aside className="character-portrait">{base ? <img src={characterApi.assetUrl(base.url)} alt={profile.name} /> : <div className="character-portrait-empty">{profile.name.slice(0, 1) || "人物"}</div>}
        <div><span>{profile.identity || "身份待定"}</span><p>{profile.tags.join(" · ")}</p></div>
      </aside>
      <div className="character-details">
        {configLoading && <p role="status">正在加载人物服务配置…</p>}
        {configError && <div role="alert">{configError}<button className="btn btn-sm" onClick={refreshConfigs}>重试加载服务配置</button></div>}
        <PersonaEditor card={selected} editable={Boolean(editable)} prepare={()=>save(false)}
          onSaved={card=>{setSelected(card);setProfile(card.profile);if(preference?.card_id===card.card_id)void selectMentor(card);}}/>
        <section className="character-section"><h2>人物设定</h2>
          <div className="character-fields"><label>名字<input value={profile.name} maxLength={80} disabled={!editable || busy} onChange={e => field("name", e.target.value)} /></label><label>身份<input value={profile.identity} maxLength={240} disabled={!editable || busy} onChange={e => field("identity", e.target.value)} /></label></div>
          <label>标签<input key={`${selected?.card_id||"new"}:${selected?.revision||0}`} defaultValue={profile.tags.join("，")} disabled={!editable || busy} onChange={e => field("tags", e.target.value.split(/[,，]/).map(value => value.trim()).filter(Boolean))} /></label>
          {([['personality', '性格'], ['background', '背景'], ['appearance', '形象描述']] as const).map(([key, label]) => <label key={key}>{label}<textarea aria-label={label} value={profile[key]} disabled={!editable || busy} rows={key === "background" ? 4 : 3} maxLength={key === "background" ? 3000 : 1200} onChange={e => field(key, e.target.value)} /></label>)}
        </section>
        <section className="character-section"><h2>声音</h2>
          <label><span className="sr">音色</span><select aria-label="音色" value={profile.voice_id} disabled={busy || !voiceConfig || !voices.length} onChange={e => {if(e.target.value)void action(()=>chooseVoice(e.target.value));}}><option value="">等待匹配人物音色</option>{profile.voice_id&&!voices.some(voice=>voice.voice_id===profile.voice_id)&&<option value={profile.voice_id}>{profile.voice_id}</option>}{voices.map(voice => <option value={voice.voice_id} key={voice.voice_id}>{voice.name}{voice.voice_id === recommended ? " · 推荐" : ""}</option>)}</select></label>
          {voiceLoading&&<p role="status">正在加载声音目录…</p>}{voiceError&&<p role="alert">{voiceError}<button className="btn" onClick={()=>loadVoices()}>重试声音目录</button></p>}
          {!recommended&&!voiceLoading&&<p className="help" role="status">{!voiceConfig?"请先在设置中配置语音服务。":voiceMatch==="no_compatible_voice"?"未找到符合人物设定的推荐音色，可手动选择。":""}</p>}
          <div aria-label="说话风格"><h3>说话风格</h3><p>{profile.speaking_style||"填写人物设定后自动生成。"}</p></div>
          <div className="row"><button className="btn" disabled={busy || voiceLoading || !voiceConfig} onClick={() => action(()=>loadVoices())}><WandSparkles size={16} />重新匹配</button>
            <button className="btn" disabled={busy || !profile.voice_id || !voiceConfig} onClick={() => action(previewVoice)}><Play size={16} />{busy&&previewRequest.current?"准备试听…":"试听"}</button></div>
          {previewUrl&&<audio ref={previewAudio} src={previewUrl} controls autoPlay aria-label="当前音色试听" onError={()=>setVoiceError("试听音频播放失败，请重新试听。")}/>}
          {selected?.assets.filter(asset => asset.kind === "voice").map(asset => <audio key={asset.asset_id} src={characterApi.assetUrl(asset.url)} controls preload="none" aria-label="人物音色试听" />)}
        </section>
        <section className="character-section"><h2>形象与动作</h2>
          <div className="character-media-grid">{(sceneAsset?[sceneAsset]:[]).map(asset => <article key={asset.asset_id} className="character-media selected">
            {asset.kind === "video" ? <video src={characterApi.assetUrl(asset.url)} controls muted playsInline preload="metadata" /> : <img src={characterApi.assetUrl(asset.url)} alt={`${profile.name} · ${scenes.find(item => item.id === asset.scene)?.name}`} />}
            <div><span>{scenes.find(item => item.id === asset.scene)?.name}</span></div>
          </article>)}</div>
          {editable && <><div className="character-fields"><label>场景<select aria-label="场景" value={scene} disabled={busy} onChange={e => setScene(e.target.value)}>{scenes.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>生成服务<select aria-label="生成服务" value={configId} disabled={busy} onChange={e => setConfigId(e.target.value)}><option value="">选择图片或视频服务</option>{configs.filter(item => ["image_generation", "video_generation"].includes(item.kind)).map(item => <option key={item.config_id} value={item.config_id}>{item.kind === "video_generation" ? "视频" : "图片"} · {item.model_name}</option>)}</select></label></div>
            {configs.find(item => item.config_id === configId)?.kind === "video_generation" && <label>时长（秒）<input type="number" min={4} max={15} value={duration} onChange={e => setDuration(Number(e.target.value))} /></label>}
            <div className="row"><button className="btn" disabled={busy || mediaPending || !selected} onClick={() => uploadRef.current?.click()}><Upload size={16} />上传素材</button><input ref={uploadRef} hidden type="file" accept="image/png,image/jpeg,image/webp,video/mp4" onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; if (file && selected) void action(()=>uploadMedia(file)); }} />
              <button className="btn" disabled={busy || voiceLoading || mediaPending || !selected || !configId} onClick={() => action(async () => { const saved=await save(); const config = configs.find(item => item.config_id === configId); await characterApi.generate(saved.card_id, { kind: config.kind === "video_generation" ? "video" : "image", revision: saved.revision, config_id: config.config_id, config_version: config.config_version, scene, count: 1, duration, ...(config.kind==="image_generation"?{replace_current:true}:{}) }); setJobs((await characterApi.jobs(saved.card_id)).data); })}><WandSparkles size={16} />{sceneAsset?"重新生成":"生成图片"}</button></div>
          </>}
          {jobs.filter(job=>["queued","leased","running"].includes(job.status)).map(job => <div key={job.task_id} className="character-job"><span role="status">{job.status==="queued"?"排队中…":"正在生成…"}</span><button className="iconbtn" aria-label="取消生成" title="取消生成" onClick={() => action(async () => { await characterApi.cancel(selected!.card_id, job.task_id); setJobs((await characterApi.jobs(selected!.card_id)).data); })}><X size={16} /></button></div>)}
        </section>
        {selected&&!editable&&<button className="btn" disabled={busy} onClick={()=>action(async()=>{const copy=await characterApi.copy(selected.card_id);go(cardPath(copy.card_id));})}>收录为自己的副本</button>}
        {selected&&!market&&<button className="btn" disabled={busy||voiceLoading||!preference||using(selected,profile.voice_id)} onClick={()=>action(async()=>selectMentor(editable?await save(false):selected))}>{using(selected,profile.voice_id)?"正在使用":"使用导师"}</button>}
        {editable && <footer className="character-save"><button className="btn btn-pri" disabled={busy || voiceLoading || !profile.name.trim()} onClick={() => action(save)}>{busy ? "处理中…" : "保存人物"}</button>
          {selected && <><a className="btn" href={characterApi.exportUrl(selected.card_id)} download>导出卡片</a><button className="btn" disabled={busy||selected.can_publish===false} title={selected.can_publish===false?"证据或人物正文尚未通过验证":undefined} onClick={() => action(async () => { const saved=await save(); await characterApi.publish(saved.card_id, saved.revision); setSelected(await characterApi.get(saved.card_id)); })}>发布到市集</button><button className="iconbtn" aria-label="删除人物" title="删除人物" disabled={busy} onClick={() => { if (window.confirm("删除此人物及其访问权限？")) void action(async () => { await characterApi.remove(selected.card_id); go("/cards"); }); }}><Trash2 size={18} /></button></>}
        </footer>}
      </div>
    </div>}
    <MarketUpload cards={cards.filter(card=>card.editable).map(card=>({card_id:card.card_id,profile:card.profile,assets:card.assets}))}
      open={marketOpen} onClose={()=>setMarketOpen(false)} onDone={message=>{setMarketOpen(false);setError("");setUseMessage(message);void refresh();}}/>
  </div>;
}
