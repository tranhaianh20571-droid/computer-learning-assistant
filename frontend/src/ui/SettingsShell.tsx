import {useEffect,useRef,useState, type ReactNode} from "react";
import {AudioLines, BookOpen, ChevronRight, LockKeyhole, ScanLine, SlidersHorizontal} from "lucide-react";
import "./settingsShell.css";
import "./settingsRevision.css";
import BrandLogo from "./BrandLogo";

export default function SettingsShell({children, name, go, onLogout,isAdmin=false}:{
  children:ReactNode;name:string;go:(path:string)=>void;onLogout:()=>void;isAdmin?:boolean;
}) {
  const [accountOpen,setAccountOpen]=useState(false);
  const [section,setSection]=useState(()=>new URLSearchParams(location.hash.split("?")[1]||"").get("section")||"models");
  const account=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    const sync=()=>{setSection(new URLSearchParams(location.hash.split("?")[1]||"").get("section")||"models");setAccountOpen(false);};
    const outside=(event:PointerEvent)=>{if(!account.current?.contains(event.target as Node)&&!(event.target as Element).closest(".settings-private-scope,.settings-account-trigger"))setAccountOpen(false);};
    window.addEventListener("hashchange",sync);document.addEventListener("pointerdown",outside);
    return ()=>{window.removeEventListener("hashchange",sync);document.removeEventListener("pointerdown",outside);};
  },[]);
  const crumb=section==="connections"?"连接与授权":section==="personal-memory"?"个人记忆":section==="admin"?"管理员 OCR":"";
  const initial=Array.from(name)[0] || "我";
  return <div className="settings-app revision-settings">
    <aside className="settings-app-nav side" aria-label="工作空间">
      <button className="settings-brand" onClick={()=>go("/home")} title="新的学习任务" aria-label="新的学习任务"><BrandLogo decorative /></button>
      <nav className="settings-workspace-links" aria-label="主导航">
        <span className="settings-nav-caption">工作空间</span>
        <button onClick={()=>go("/courses")} title="课程"><BookOpen/><span>课程</span></button>
        <button onClick={()=>go("/materials")} title="我的资料"><ScanLine/><span>我的资料</span></button>
        <button onClick={()=>go("/cards")} title="人物导师"><AudioLines/><span>人物导师</span></button>
      </nav>
      <div className="settings-app-bottom">
        <button className="settings-current" onClick={()=>go("/capabilities")} title="设置" aria-current="page"><SlidersHorizontal/><span>设置</span></button>
        <div className="settings-account">
          <button className="settings-account-trigger" aria-label="个人工作空间菜单" aria-expanded={accountOpen} onClick={()=>setAccountOpen(value=>!value)}>
            <span className="settings-avatar">{initial}</span><span className="settings-account-name">{name}<small>个人工作空间</small></span>
          </button>
        </div>
      </div>
    </aside>
    <div className="settings-workspace">
      <header className="settings-topbar"><nav className="settings-breadcrumb" aria-label="面包屑"><button className="settings-workspace-crumb" onClick={()=>go("/home")}>个人工作空间</button><ChevronRight/><button onClick={()=>go("/capabilities")} aria-current={crumb?undefined:"page"}>设置</button>{crumb&&<><ChevronRight/><span aria-current="page">{crumb}</span></>}</nav><button className="settings-private-scope" aria-label="个人设置菜单" aria-expanded={accountOpen} onClick={()=>setAccountOpen(value=>!value)}><LockKeyhole/>个人设置</button></header>
      <main className="settings-scroll">{children}</main>
    </div>
    {accountOpen&&<div className="settings-account-menu" ref={account} onKeyDown={event=>{if(event.key==="Escape")setAccountOpen(false);}}>
      <button onClick={()=>{setAccountOpen(false);go("/capabilities?section=connections");}}>连接与授权</button>
      <button onClick={()=>{setAccountOpen(false);go("/capabilities?section=personal-memory");}}>个人记忆</button>
      <button onClick={()=>{setAccountOpen(false);go("/capabilities?section=storage");}}>存储与性能</button>
      {isAdmin&&<button onClick={()=>{setAccountOpen(false);go("/capabilities?section=admin");}}>管理员 OCR</button>}
      <button onClick={onLogout}>退出登录</button>
    </div>}
  </div>;
}
