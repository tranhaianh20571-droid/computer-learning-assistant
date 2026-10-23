import {useEffect, useRef, useState, type JSX} from "react";
import {LexicalComposer} from "@lexical/react/LexicalComposer";
import {PlainTextPlugin} from "@lexical/react/LexicalPlainTextPlugin";
import {ContentEditable} from "@lexical/react/LexicalContentEditable";
import {HistoryPlugin} from "@lexical/react/LexicalHistoryPlugin";
import {OnChangePlugin} from "@lexical/react/LexicalOnChangePlugin";
import {LexicalErrorBoundary} from "@lexical/react/LexicalErrorBoundary";
import {useLexicalComposerContext} from "@lexical/react/LexicalComposerContext";
import {$createTextNode, $getNodeByKey, $getRoot, $getSelection, $isRangeSelection, $nodesOfType,
  COMMAND_PRIORITY_HIGH, DecoratorNode, PASTE_COMMAND, type EditorConfig, type LexicalNode,
  type NodeKey, type SerializedLexicalNode, type Spread} from "lexical";
import {Copy,ExternalLink,Link2,Pencil,Trash2} from "lucide-react";
import "./sourceComposer.css";

type SerializedLink = Spread<{url:string; type:"source-link"; version:1}, SerializedLexicalNode>;
class SourceLinkNode extends DecoratorNode<JSX.Element> {
  __url:string;
  static getType(){return "source-link";}
  static clone(node:SourceLinkNode){return new SourceLinkNode(node.__url,node.__key);}
  constructor(url:string,key?:NodeKey){super(key);this.__url=url;}
  createDOM(_config:EditorConfig){const span=document.createElement("span");span.className="source-link-wrap";return span;}
  updateDOM(){return false;}
  isInline(){return true;}
  getTextContent(){return this.__url;}
  exportJSON():SerializedLink{return {...super.exportJSON(),type:"source-link",version:1,url:this.__url};}
  static importJSON(value:SerializedLink){return new SourceLinkNode(value.url);}
  decorate(){
    return <SourceLinkView url={this.__url} nodeKey={this.__key}/>;
  }
}

function SourceLinkView({url,nodeKey}:{url:string;nodeKey:NodeKey}){
  const [editor]=useLexicalComposerContext();
  const [opened,setOpened]=useState(false),[editing,setEditing]=useState(false),[draft,setDraft]=useState(url);
  let label=url;
  try {const parsed=new URL(url);label=parsed.host+parsed.pathname+(parsed.search?"?…":"");} catch {/* retain safe label */}
  function update(value?:string){
    editor.update(()=>{
      const node=$getNodeByKey(nodeKey);
      if(!(node instanceof SourceLinkNode))return;
      if(value)node.replace(new SourceLinkNode(value));else node.remove();
    });
    setOpened(false);setEditing(false);editor.focus();
  }
  return <span className="source-link-control" onBlur={event=>{
    if(!event.currentTarget.contains(event.relatedTarget)){setOpened(false);setEditing(false);}
  }} onKeyDown={event=>{if(event.key==="Escape"){setOpened(false);setEditing(false);}}}>
    <a className="source-inline-link" href={url} title={url} target="_blank" rel="noopener noreferrer"
      onClick={event=>{if(!(event.ctrlKey||event.metaKey)){event.preventDefault();setOpened(!opened);}}}>
      <Link2 size={14}/><span>{label}</span></a>
    {opened&&<span className="source-link-tools" contentEditable={false}>
      {editing?<input aria-label="编辑链接地址" type="url" value={draft} onChange={event=>setDraft(event.target.value)}
        onKeyDown={event=>{if(event.key==="Enter"){event.preventDefault();event.stopPropagation();const value=safeUrl(draft);if(value)update(value);}}}/>:
        <>
          <a className="iconbtn" title="打开链接" aria-label="打开链接" href={url} target="_blank" rel="noopener noreferrer"><ExternalLink size={14}/></a>
          <button className="iconbtn" type="button" title="复制链接" onClick={()=>void navigator.clipboard.writeText(url).catch(()=>{})}><Copy size={14}/></button>
          <button className="iconbtn" type="button" title="编辑链接" onClick={()=>{setDraft(url);setEditing(true);}}><Pencil size={14}/></button>
          <button className="iconbtn" type="button" title="移除链接" onClick={()=>update()}><Trash2 size={14}/></button>
        </>}
    </span>}
  </span>;
}

function safeUrl(text:string){
  try {const url=new URL(text);return /^https?:$/.test(url.protocol)&&!url.username&&!url.password?url.href:null;}
  catch{return null;}
}
function PastePlugin({onFiles,disabled}:{onFiles:(files:File[])=>void;disabled:boolean}){
  const [editor]=useLexicalComposerContext();
  const files=useRef(onFiles);
  useEffect(()=>{files.current=onFiles;},[onFiles]);
  useEffect(()=>{editor.setEditable(!disabled);},[editor,disabled]);
  useEffect(()=>editor.registerCommand(PASTE_COMMAND,event=>{
    if(disabled||!(event instanceof ClipboardEvent)||!event.clipboardData)return false;
    const pasted=Array.from(event.clipboardData.files);
    if(pasted.length){event.preventDefault();files.current(pasted);return true;}
    let text=event.clipboardData.getData("text/plain");
    if(!text&&event.clipboardData.getData("text/html")){
      const parsed=new DOMParser().parseFromString(event.clipboardData.getData("text/html"),"text/html");
      parsed.querySelectorAll("script,style,iframe").forEach(node=>node.remove());
      parsed.querySelectorAll("a[href]").forEach(anchor=>{
        const href=safeUrl(anchor.getAttribute("href")||"");
        if(href)anchor.replaceWith(document.createTextNode(href));
      });
      text=parsed.body.textContent||"";
    }
    if(!/https?:\/\//i.test(text))return false;
    const selection=$getSelection();
    if(!$isRangeSelection(selection))return false;
    event.preventDefault();
    const nodes:LexicalNode[]=[];
    const pattern=/https?:\/\/[^\s<>"\u3000]+/gi;
    let cursor=0,match:RegExpExecArray|null;
    while((match=pattern.exec(text))){
      const raw=match[0].replace(/[，。；！、）)\]}]+$/g,"");
      const url=safeUrl(raw);
      if(!url)continue;
      if(match.index>cursor)nodes.push($createTextNode(text.slice(cursor,match.index)));
      nodes.push(new SourceLinkNode(url));
      cursor=match.index+raw.length;
    }
    if(cursor<text.length)nodes.push($createTextNode(text.slice(cursor)));
    selection.insertNodes(nodes);
    return true;
  },COMMAND_PRIORITY_HIGH),[editor,disabled]);
  return null;
}

export default function SourceComposer({label,placeholder,onChange,onFiles,onSubmit,disabled=false}:{
  label:string;placeholder?:string;onChange:(text:string,links:string[])=>void;
  onFiles:(files:File[])=>void;onSubmit?:()=>void;disabled?:boolean;
}){
  return <LexicalComposer initialConfig={{namespace:"source-composer",nodes:[SourceLinkNode],
    onError:()=>{},theme:{paragraph:"source-paragraph"}}}>
    <div className="source-editor" onKeyDown={event=>{
      if(event.key==="Enter"&&(event.ctrlKey||event.metaKey)&&!event.nativeEvent.isComposing){
        event.preventDefault();onSubmit?.();
      }
    }}>
      <PlainTextPlugin contentEditable={<ContentEditable className="source-editable" aria-label={label}
        aria-placeholder={placeholder||" "} placeholder={<span className="source-placeholder">{placeholder}</span>}/>}
        placeholder={null} ErrorBoundary={LexicalErrorBoundary}/>
      <HistoryPlugin/>
      <OnChangePlugin onChange={state=>state.read(()=>onChange($getRoot().getTextContent(),
        [...new Set($nodesOfType(SourceLinkNode).map(node=>node.__url))]))}/>
      <PastePlugin onFiles={onFiles} disabled={disabled}/>
    </div>
  </LexicalComposer>;
}
