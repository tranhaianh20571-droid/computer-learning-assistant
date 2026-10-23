import {useEffect,useState} from "react";
import {Check,Eye,EyeOff,LockKeyhole} from "lucide-react";
import {api,sourceApi} from "./api";
import InfoHint from "./ui/InfoHint";
import {PurposeConnection,type Binding} from "./ui/PurposeModelSettings";
import "./ui/sourceComposer.css";

export default function SourceProviderSettings({provider}:{provider:"firecrawl"|"bilinote"}){
  const video=provider==="bilinote";
  const [profile,setProfile]=useState<any>(null);
  const [endpoint,setEndpoint]=useState(video?"builtin://bilinote":"https://api.firecrawl.dev");
  const [key,setKey]=useState("");
  const [reveal,setReveal]=useState(false);
  const [options,setOptions]=useState<any>({});
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");
  const [bindings,setBindings]=useState<Binding[]>([]);
  useEffect(()=>{
    let alive=true;
    sourceApi.settings(provider).then(value=>{
      if(alive){setProfile(value);setEndpoint(value.configuration?.endpoint||(video?"builtin://bilinote":"https://api.firecrawl.dev"));setOptions(value.options);}
    }).catch(()=>{if(alive)setError("配置加载失败，请刷新后重试。");});
    if(video)api.modelRoles().then(value=>{if(alive)setBindings(value.data);}).catch(()=>{});
    return ()=>{alive=false;setKey("");setReveal(false);};
  },[provider,video]);
  async function perform(test=false){
    if(busy||!profile)return;
    setBusy(true);setError("");setNotice("");
    try{
      const next=await sourceApi.saveSettings(provider,{expected_revision:profile.revision,endpoint,
        ...(key?{api_key:key}:{}),options});
      setProfile(next);setKey("");setReveal(false);
      if(test){
        const result=await sourceApi.testSettings(provider);
        setNotice(result.connection==="available"?"连接可用；实际解析效果另行验证。":"已保存，请检查视频服务部署状态。");
      }else setNotice("已保存，新任务将使用此配置。");
    }catch(e:any){setError(e.status===409?"配置已发生变化，请刷新后重试。":e.message||"保存失败，请检查配置。");}
    finally{setBusy(false);}
  }
  const model=bindings.find(row=>row.role==="video_understanding");
  const name=video?"BiliNote":"Firecrawl";
  const toggle=(field:string,label:string,help:string)=><label key={field}>
    <input type="checkbox" aria-label={label} checked={options[field]??true} disabled={busy}
      onChange={event=>setOptions((old:any)=>({...old,[field]:event.target.checked}))}/>{label}
    <InfoHint label={label}>{help}</InfoHint></label>;
  const number=(field:string,label:string,min:number,max:number,help:string)=><label key={field}>{label}
    <InfoHint label={label}>{help}</InfoHint><input type="number" min={min} max={max} value={options[field]??min}
      disabled={busy} onChange={event=>setOptions((old:any)=>({...old,[field]:Number(event.target.value)}))}/></label>;
  return <div className="purpose-settings">
    <header className="purpose-heading"><h2 aria-label={name}>{name}<InfoHint label={name}>{video?
      "解析视频字幕、音频和截图。截图与抽帧理解默认开启，多模态模型独立配置，可能产生模型或转写费用。":
      "抓取当前网页或你勾选的目录章节。网页地址将发送到所配置的抓取服务，可能使用账号额度。"}</InfoHint></h2></header>
    {!profile?<p role="status">{error||"正在加载…"}</p>:<section className="purpose-connection">
      <form onSubmit={event=>{event.preventDefault();void perform();}}>
        <div className="purpose-field"><label htmlFor={provider+"-endpoint"}>API 地址<InfoHint label="API 地址">
          {video?"内置视频服务使用 builtin://bilinote；自部署兼容网关填写基础地址。服务令牌与下方模型密钥分别保存。":
          "填写 Firecrawl 基础地址。云端 API 使用本人密钥；内网服务必须由部署管理员登记。"}</InfoHint></label>
          <input id={provider+"-endpoint"} value={endpoint} spellCheck={false} disabled={busy} onChange={event=>setEndpoint(event.target.value)}/></div>
        <div className="purpose-field"><label htmlFor={provider+"-key"}>{video?"服务令牌":"API Key"}<InfoHint label="API Key">
          密钥加密保存在服务端，留空保留；更换地址时请重新填写。此处显隐仅用于当前输入的新密钥。</InfoHint></label>
          <div className="purpose-secret"><input id={provider+"-key"} type={reveal?"text":"password"} value={key}
            autoComplete="off" placeholder={profile.configuration?.credential_mask||(video?"内置服务无需填写":"请输入 API Key")}
            disabled={busy} onChange={event=>setKey(event.target.value)}/>
            <button type="button" className="iconbtn" aria-label={reveal?"隐藏密钥":"显示密钥"} onClick={()=>setReveal(!reveal)}>{reveal?<EyeOff size={16}/>:<Eye size={16}/>}</button></div></div>
        <div className="source-settings-options">
          {toggle("enabled","启用服务","停用后不创建新解析，已保存的资料仍按原有权限读取。")}
          {video?<>
            {toggle("screenshot","视频截图","保存带时间定位的截图，仅本人可以查看。")}
            {toggle("link","原片时间跳转","将识别片段关联到原视频对应时间。")}
            {toggle("video_understanding","画面理解","将抽取画面实际发送给下方多模态模型；已有字幕时也处理画面。")}
            {number("interval","抽帧间隔（秒）",1,120,"间隔越小，画面越密集，处理时间与用量也会增加。")}
            {number("grid_size","拼图边长",1,4,"每组画面网格边长；文字细小时会保留单帧输入。")}
            {number("max_frames","最多处理帧数",1,600,"限制单视频图片用量。达到上限会标记采样覆盖范围，不宣称看完全部画面。")}
            <label>转写模型<InfoHint label="转写模型">没有有效字幕时使用本地 Whisper 转写，模型越大通常需要更多内存和处理时间。</InfoHint>
              <select value={options.asr_model||"small"} onChange={event=>setOptions((old:any)=>({...old,asr_model:event.target.value}))}>
                {["tiny","base","small","medium","large-v3"].map(value=><option key={value}>{value}</option>)}</select></label>
          </>:<>
            {toggle("keep_images","保留正文图片","仅处理受控正文图片，图片内容不会直接在浏览器外链加载。")}
            {number("wait_ms","页面等待（毫秒）",0,10000,"为动态正文预留渲染时间，服务版本需支持此选项。")}
            {number("timeout","单页超时（秒）",5,120,"包含网页处理等待，不代表必定在此时间内成功。")}
          </>}
        </div>
        {error&&<p role="alert" className="capability-detail">{error}</p>}
        <div className="purpose-save"><span className="help" role="status">{notice&&<Check size={14}/>} {notice}</span>
          <div><button type="button" className="btn" disabled={busy} onClick={()=>void perform(true)}>测试连接</button>
          <button type="submit" className="btn btn-pri" disabled={busy}>{busy?"处理中…":"保存"}</button></div></div>
      </form><p className="purpose-scope help"><LockKeyhole size={13}/>仅用于你的工作空间</p>
    </section>}
    {video&&model&&<><header className="purpose-heading"><h2 aria-label="多模态模型">多模态模型<InfoHint label="多模态模型">
      需要支持图像输入，用于读取截图、公式和板书。思考强度按当前连接检测；保存与调用不以测试通过为前提。</InfoHint></h2></header>
      <PurposeConnection binding={model} name="视频理解" visible onUpdate={setBindings} onSaved={async()=>{}}/>
    </>}
  </div>;
}
