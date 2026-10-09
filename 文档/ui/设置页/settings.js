'use strict';
// Offline discovery prototype. All state is synthetic and lives only in memory.
const I = name => window.SETTINGS_ICONS[name] || '';
const esc = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const state = { section:'models', category:'text', selected:'a', busy:false, models:[
  {id:'a',kind:'text',provider:'Gemini',protocol:'Gemini 原生',name:'gemini-2.5-pro',endpoint:'https://generativelanguage.googleapis.com/v1beta',status:'ready',caps:['文本','图像识别','流式','工具调用'],checked:'刚刚',delay:'1.24 s'},
  {id:'b',kind:'text',provider:'OpenAI',protocol:'OpenAI 兼容',name:'gpt-4.1',endpoint:'https://api.openai.com/v1',status:'unknown',caps:['文本','JSON Schema'],checked:'未测试',delay:'—'},
  {id:'c',kind:'image',provider:'OpenAI',protocol:'OpenAI 兼容图片生成',name:'gpt-image-1',endpoint:'https://api.openai.com/v1',status:'ready',caps:['图片生成'],checked:'2 分钟前',delay:'8.60 s'},
  {id:'d',kind:'voice',provider:'MiniMax',protocol:'MiniMax TTS',name:'speech-2.8-hd',endpoint:'https://api.minimaxi.com',status:'error',caps:['语音合成'],checked:'5 分钟前',delay:'—',voice:'清澈女声'}
]};
const sections=[['models','Cpu','模型与语音','01'],['devices','Monitor','本机连接器','02'],['privacy','ShieldCheck','外发授权','03'],['admin','ScanLine','云端能力','04']];
const categories=[['text','Cpu','内容模型'],['image','Image','封面图片'],['voice','AudioLines','语音合成']];
let lastFocus = null;
let dialogEpoch = 0;
const app = document.querySelector('#app');
const modal = document.querySelector('#dialog');
const statusNames={ready:'可用',unknown:'待测试',error:'连接失败'};
function status(model){return `<span class="status ${model.status}"><span class="status-dot"></span>${statusNames[model.status]}</span>`;}
function iconButton(icon,label,attrs=''){return `<button class="icon-button" type="button" aria-label="${esc(label)}" title="${esc(label)}" ${attrs}>${I(icon)}</button>`;}
function toast(text){ const node=document.querySelector('#toast'); node.textContent=text; node.classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>node.classList.remove('show'),2800); }
function clearToast(){const node=document.querySelector('#toast');clearTimeout(toast.timer);node.classList.remove('show');node.textContent='';}
function render(){
  const titles={models:['模型与语音','个人能力'],devices:['本机连接器','设备'],privacy:['外发授权','隐私'],admin:['云端能力','管理员']};
  const current=titles[state.section];
  app.innerHTML=`<header class="app-header"><a class="brand" href="index.html" aria-label="学习助手伙伴 · 设计方向">${I('BookOpen')}<span>学习助手伙伴</span></a><span class="context">个人工作区</span><div class="header-end"><span class="prototype-mark">设计预览</span><span class="avatar" aria-label="演示用户">林</span></div></header>
  <div class="shell"><aside class="sidebar"><div class="sidebar-title">设置<span class="sidebar-sub">PREFERENCES</span></div><nav aria-label="设置分类">${sections.map(([key,icon,label,num])=>`<button type="button" class="side-link ${state.section===key?'active':''}" data-section="${key}" aria-current="${state.section===key?'page':'false'}"><span class="nav-number" aria-hidden="true">${num}</span>${I(icon)}<span>${label}</span>${I('ChevronRight')}</button>`).join('')}</nav><div class="sidebar-foot">${I('LockKeyhole')}<span>凭据仅显示掩码</span></div></aside>
  <main id="main" tabindex="-1"><div class="page-top"><div><div class="eyebrow">设置 <span>/</span> ${current[1]}</div><h1>${current[0]}</h1></div><div class="page-actions">${state.section==='models'?`${iconButton('RefreshCw','刷新配置','data-action="refresh"')}<button class="primary" data-action="add">${I('Plus')}添加配置</button>`:''}</div></div>
  ${state.section==='models'?modelsView():state.section==='devices'?devicesView():state.section==='privacy'?privacyView():adminView()}
  <footer class="page-foot"><span>学习助手伙伴</span><span>个人配置 · 合成示例</span></footer></main></div>`;
}
function modelsView(){
  const rows=state.models.filter(m=>m.kind===state.category);
  let selected=rows.find(m=>m.id===state.selected) || rows[0];
  if(selected) state.selected=selected.id;
  const usable=state.models.filter(m=>m.status==='ready').length;
  return `<div class="overview-strip"><div class="overview-heading"><span class="section-label">连接概况</span><strong>${usable}<small> / ${state.models.length}</small></strong><span>配置可用</span></div><div class="overview-signals">${categories.map(([kind,icon,label])=>{const list=state.models.filter(m=>m.kind===kind); return `<span>${I(icon)}${label}<b class="${list.some(m=>m.status==='ready')?'positive':'warning'}">${list.some(m=>m.status==='ready')?'已连接':list.length?'需检查':'未配置'}</b></span>`;}).join('')}</div></div>
  <div class="workspace"><section class="config-list" aria-label="模型配置"><div class="category-tabs" role="tablist" aria-label="能力类型">${categories.map(([key,icon,label])=>`<button role="tab" id="tab-${key}" aria-selected="${state.category===key}" tabindex="${state.category===key?'0':'-1'}" aria-controls="model-panel" data-category="${key}">${I(icon)}${label}<span>${state.models.filter(m=>m.kind===key).length}</span></button>`).join('')}</div>
  <div id="model-panel" role="tabpanel" aria-labelledby="tab-${state.category}"><div class="list-heading"><span>已保存配置</span><span>连接状态</span></div>${rows.length?rows.map(m=>`<button class="model-row ${m.id===state.selected?'selected':''}" data-select="${m.id}" aria-pressed="${m.id===state.selected}"><span class="provider-symbol ${m.kind}">${I(m.kind==='image'?'Image':m.kind==='voice'?'AudioLines':'Cpu')}</span><span class="model-identity"><span class="provider">${esc(m.provider)}<small>${esc(m.protocol)}</small></span><strong>${esc(m.name)}</strong>${m.id===rows[rows.length-1].id?'<span class="in-use">后续任务使用</span>':''}</span><span class="model-right">${status(m)}${I('ChevronRight')}</span></button>`).join(''):`<div class="empty">${I('Plus')}<h2>尚无${categories.find(c=>c[0]===state.category)[2]}配置</h2><button class="primary" data-action="add">添加配置</button></div>`}</div>
  <div class="list-note">${I('CircleHelp')}<span>同类配置使用最近新增的一条。</span></div></section>
  ${selected?detailView(selected):''}</div>
  <section class="security-line">${I('ShieldCheck')}<div><strong>凭据与外发</strong><p>凭据仅显示掩码。此预览不保存凭据、不调用外部服务；实际图片与语音测试可能计费。</p></div><button class="text-button" data-section="privacy">授权记录${I('ArrowUpRight')}</button></section>`;
}
function detailView(m){
  return `<section class="inspector" aria-label="配置详情"><div class="inspector-top">${document.body.classList.contains('ink')?iconButton('ArrowLeft','返回配置列表','data-action="back-list"'):''}<span class="section-label">配置详情</span>${iconButton('Pencil','编辑配置','data-action="edit"')}</div><div class="detail-title"><span>${esc(m.provider)}</span><h2>${esc(m.name)}</h2>${status(m)}</div><dl><div><dt>协议</dt><dd>${esc(m.protocol)}</dd></div><div><dt>服务地址</dt><dd class="mono endpoint">${esc(m.endpoint)}</dd></div><div><dt>API Key</dt><dd class="mono key">•••• •••• •••• DEMO ${I('LockKeyhole')}</dd></div>${m.kind==='voice'?`<div><dt>当前音色</dt><dd>${m.voice||'清澈女声'}</dd></div>`:''}</dl>
  ${m.kind==='image'?`<div class="cover-preview"><button class="preview-open" data-action="preview" aria-label="放大封面样例"><img src="ink-sample.png" alt="合成水墨树形封面样例"></button><span>封面样例 · 数据结构</span></div>`:''}
  <div class="capabilities"><h3>能力探测</h3>${m.caps.map(cap=>`<div class="capability-line"><span>${cap}</span><span class="${m.status==='ready'?'positive':m.status==='error'?'negative':'muted'}">${I(m.status==='ready'?'Check':m.status==='error'?'CircleAlert':'CircleHelp')}${m.status==='ready'?'已通过':m.status==='error'?'未通过':'待测试'}</span></div>`).join('')}</div>
  ${m.status==='error'?`<div class="inline-error" role="alert">${I('CircleAlert')}<div><strong>连接超时 · 演示</strong><p>阶段：建立连接。请检查服务地址与凭据。</p></div><button class="text-button" data-action="edit">编辑配置${I('ArrowUpRight')}</button></div>`:''}<div class="test-meta"><span>最近测试 · ${m.checked}</span><span class="mono">${m.delay}</span></div><div class="detail-actions"><button class="test-button" data-action="test">${I('Play')}测试能力</button>${iconButton('Trash2','删除配置','data-action="delete"')}</div></section>`;
}
function devicesView(){return `<section class="section-band"><div class="band-title"><h2>已配对设备</h2><button class="primary" data-action="pair">${I('Plus')}配对设备</button></div><div class="device-row">${I('Monitor')}<div><strong>我的学习电脑</strong><p class="muted">最近在线 · 刚刚</p></div><span class="status ready"><span class="status-dot"></span>在线</span><button class="text-button negative" data-action="revoke-device">撤销配对</button></div><div class="device-row">${I('Monitor')}<div><strong>书房笔记本</strong><p class="muted">最近在线 · 昨天</p></div><span class="status unknown"><span class="status-dot"></span>离线</span><button class="text-button negative" data-action="revoke-device">撤销配对</button></div><p class="list-note">${I('LockKeyhole')}配对码一次性使用，10 分钟后失效。</p></section>`;}
function privacyView(){return `<section class="section-band"><div class="band-title"><h2>按任务查询授权</h2></div><form id="privacy-form" class="query-row"><label>任务 ID<input name="task" required placeholder="输入任务 ID" value="task_demo_01"></label><button class="primary">${I('Search')}查询</button></form><div id="grants"><div class="grant-row"><div>${I('Image')}<strong>封面图片生成</strong><p class="muted">课程主题与画面描述</p></div><span class="status ready"><span class="status-dot"></span>有效</span><button class="text-button negative" data-action="revoke-grant">撤销授权</button></div></div></section><section class="security-line">${I('ShieldCheck')}<div><strong>授权由你决定</strong><p>撤销后，后续任务将停止外发。已经发出的内容无法收回。</p></div></section>`;}
function adminView(){return `<section class="section-band"><div class="band-title"><h2>管理员云能力</h2><span class="section-label">仅管理员</span></div><div class="device-row">${I('ScanLine')}<div><strong>云端 OCR</strong><p class="mono muted">PaddleOCR-VL-1.6</p></div><span class="status unknown"><span class="status-dot"></span>未配置</span><button class="secondary" data-action="cloud">配置</button></div><div class="device-row">${I('Search')}<div><strong>联网搜索</strong><p class="muted">Tavily Hikari</p></div><span class="status unknown"><span class="status-dot"></span>未配置</span><button class="secondary" data-action="cloud">配置</button></div></section>`;}
function openDialog(title,body,actions){
  dialogEpoch++;
  lastFocus=document.activeElement;
  modal.setAttribute('aria-labelledby','modal-title');
  modal.innerHTML=`<div class="modal-top"><h2 id="modal-title">${title}</h2>${iconButton('X','关闭','data-close')}</div>${body}<div class="modal-actions">${actions}</div>`;
  modal.showModal();
}
function editor(edit=false,cloud=false){
  const m=edit?state.models.find(x=>x.id===state.selected):null;
  openDialog(edit?'编辑配置':cloud?'添加云端能力':'添加配置',`<form id="editor"><label>能力类型<select name="kind" ${edit?'disabled':''}>${categories.map(([key,,name])=>`<option value="${key}" ${(m?.kind||state.category)===key?'selected':''}>${name}</option>`).join('')}${cloud?'<option value="ocr" selected>云端 OCR</option><option value="search">联网搜索</option>':''}</select></label><label>协议<select name="protocol"><option>${esc(m?.protocol||'OpenAI 兼容')}</option><option>Gemini 原生</option><option>Anthropic 原生</option><option>MiniMax TTS</option></select></label><label>服务地址<input name="endpoint" type="url" value="${esc(m?.endpoint||'https://api.example.com/v1')}" required></label><label>模型名<input name="model" value="${esc(m?.name||'')}" placeholder="输入模型名称" required></label><label>API Key<div class="password-field"><input name="key" type="password" autocomplete="off" placeholder="${edit?'留空保留现有凭据':'仅填写演示数据'}" ${edit?'':'required'}>${iconButton('Eye','显示或隐藏凭据','data-action="visibility"')}</div></label><p class="muted">${edit?'API Key 留空会保留现有凭据。':'此演示不保存凭据，也不会调用外部服务。'}</p><p id="form-error" role="alert"></p></form>`,`<button class="secondary" data-close>取消</button><button class="primary" type="submit" form="editor">保存配置</button>`);
  document.querySelector('#editor').onsubmit=e=>{e.preventDefault(); if(state.busy)return; state.busy=true; const values=new FormData(e.target); const submit=modal.querySelector('[type=submit]'); submit.disabled=true;submit.textContent='正在保存…';setTimeout(()=>{if(cloud){toast('演示配置已保存');}else if(m){m.name=String(values.get('model'));m.endpoint=String(values.get('endpoint'));m.protocol=String(values.get('protocol'));m.provider=m.protocol.split(' ')[0];m.status='unknown';m.checked='未测试';toast('配置已更新');}else{const kind=String(values.get('kind')); const item={id:'demo'+Date.now(),kind,provider:String(values.get('protocol')).split(' ')[0],protocol:String(values.get('protocol')),name:String(values.get('model')),endpoint:String(values.get('endpoint')),status:'unknown',caps:kind==='voice'?['语音合成']:kind==='image'?['图片生成']:['文本'],checked:'未测试',delay:'—'};state.models.push(item);state.category=kind;state.selected=item.id;toast('配置已添加');}state.busy=false;closeDialog();render();},700);};
}
function closeDialog(){if(state.busy)return;modal.close();lastFocus?.focus();}
function testModel(){
  const m=state.models.find(x=>x.id===state.selected);let cancelled=false; const epoch=dialogEpoch+1;
  openDialog('测试能力',`<div class="test-progress"><span class="section-label">${esc(m.name)}</span><p id="test-text">正在建立连接…</p><progress max="100" value="15"></progress><span class="muted" id="elapsed">已用时 0 秒</span></div>`,`<button class="secondary" id="cancel-test">取消测试</button>`);
  const start=Date.now();const timer=setInterval(()=>{if(cancelled||!modal.open||dialogEpoch!==epoch){clearInterval(timer);return;}const elapsed=Math.floor((Date.now()-start)/1000);document.querySelector('#elapsed').textContent=`已用时 ${elapsed} 秒`;modal.querySelector('progress').value=Math.min(95,15+elapsed*23);document.querySelector('#test-text').textContent=elapsed>=1?'正在验证模型能力…':'正在建立连接…';},300);
  document.querySelector('#cancel-test').onclick=()=>{cancelled=true;clearInterval(timer);closeDialog();toast('测试已取消');};
  setTimeout(()=>{clearInterval(timer);if(cancelled||!modal.open||dialogEpoch!==epoch)return;if(m.status==='error'){m.checked='刚刚';render();modal.querySelector('.test-progress').innerHTML=`<div class="negative">${I('CircleAlert')}</div><h3>连接失败</h3><p>连接超时 · 建立连接阶段</p><p>请检查服务地址与凭据后重试。</p><p class="muted">最近测试 · 刚刚；本次为合成失败示例。</p>`;modal.querySelector('.modal-actions').innerHTML='<button class="secondary" data-close>关闭</button><button class="primary" data-action="edit">修改配置</button>';}else{m.status='ready';m.checked='刚刚';m.delay='1.24 s';closeDialog();render();toast(`${m.name} · 能力测试通过`);}},2700);
}
app.addEventListener('click',handleClick);modal.addEventListener('click',handleClick);
function handleClick(e){
  const button=e.target.closest('button');if(!button)return;
  if(button.hasAttribute('data-close')){closeDialog();return;}
  if(button.dataset.section){clearToast();state.section=button.dataset.section;render();app.querySelector('.side-link.active')?.focus();return;}
  if(button.dataset.category){clearToast();state.category=button.dataset.category;render();app.querySelector(`[data-category="${state.category}"]`)?.focus();return;}
  if(button.dataset.select){clearToast();state.selected=button.dataset.select;render();const compact=document.body.classList.contains('ink')&&matchMedia('(max-width:950px)').matches;const target=compact?app.querySelector('.detail-title h2'):app.querySelector(`[data-select="${state.selected}"]`);if(compact){target.setAttribute('tabindex','-1');target.focus({preventScroll:true});app.querySelector('.inspector').scrollIntoView({block:'start',behavior:'instant'});}else target?.focus();return;}
  const action=button.dataset.action;
  if(action==='back-list'){const target=app.querySelector(`[data-select="${state.selected}"]`);target?.focus({preventScroll:true});app.querySelector('.config-list')?.scrollIntoView({block:'start',behavior:'instant'});return;}
  if(action==='add')editor();
  if(action==='edit'){if(modal.open)modal.close();editor(true);}
  if(action==='cloud')editor(false,true);
  if(action==='visibility'){const input=modal.querySelector('[name=key]');input.type=input.type==='password'?'text':'password';button.innerHTML=I(input.type==='password'?'Eye':'EyeOff');}
  if(action==='refresh'){button.disabled=true;button.setAttribute('aria-busy','true');setTimeout(()=>{render();toast('配置已刷新');},600);}
  if(action==='preview')openDialog('封面样例 · 数据结构','<img class="image-full" src="ink-sample.png" alt="合成水墨树形封面样例">','<button class="secondary" data-close>关闭</button>');
  if(action==='test'){clearToast();testModel();}
  if(action==='delete'){
    const m=state.models.find(x=>x.id===state.selected);openDialog('删除配置',`<p>确定删除 <strong>${esc(m.name)}</strong>？</p><p class="muted">后续任务将停止使用该配置。</p>`,`<button class="secondary" data-close>取消</button><button class="danger" id="confirm-delete">确认删除</button>`);
    document.querySelector('#confirm-delete').onclick=()=>{state.models=state.models.filter(x=>x.id!==m.id);closeDialog();render();toast('配置已删除');};
  }
  if(action==='pair')openDialog('配对本机连接器',`<label>设备名<input id="device-name" placeholder="例如：书房电脑"></label><div class="pair-code"><span class="section-label">一次性配对码</span><strong>DEMO-4812</strong><p class="muted">演示配对码 · 不可连接真实设备</p></div>`,`<button class="primary" data-close>完成</button>`);
  if(action==='revoke-device'||action==='revoke-grant'){
    const row=button.closest('.device-row,.grant-row');openDialog('确认撤销',`<p>${action==='revoke-device'?'撤销后，该设备将停止访问本机能力。':'撤销后，任务将停止后续外发。已经发出的内容无法收回。'}</p>`,`<button class="secondary" data-close>取消</button><button class="danger" id="confirm-revoke">确认撤销</button>`);document.querySelector('#confirm-revoke').onclick=()=>{closeDialog();row.querySelector('.status').className='status unknown';row.querySelector('.status').textContent='已撤销';button.remove();toast('已撤销');};
  }
}
app.addEventListener('submit',e=>{if(e.target.id==='privacy-form'){e.preventDefault();const node=document.querySelector('#grants');node.innerHTML='<p class="muted" role="status">正在查询授权…</p>';setTimeout(()=>{node.innerHTML='<div class="empty"><h2>未找到该任务的有效授权</h2><p class="muted">检查任务 ID 后重新查询。</p></div>';},600);}});
app.addEventListener('keydown',e=>{if(e.target.matches('[role=tab]')&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();clearToast();const index=categories.findIndex(c=>c[0]===state.category);const next=e.key==='Home'?0:e.key==='End'?categories.length-1:(index+(e.key==='ArrowRight'?1:categories.length-1))%categories.length;state.category=categories[next][0];render();app.querySelector(`[data-category="${state.category}"]`).focus();}});
modal.addEventListener('keydown',e=>{if(e.key!=='Tab')return;const focusable=[...modal.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[href]')].filter(el=>el.offsetParent!==null);if(!focusable.length)return;const first=focusable[0],last=focusable[focusable.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}});
modal.addEventListener('cancel',e=>{if(state.busy)e.preventDefault();});
modal.addEventListener('close',()=>{if(modal.open)return;dialogEpoch++; requestAnimationFrame(()=>{if(modal.open)return;if(lastFocus?.isConnected)lastFocus.focus();else app.querySelector('[data-action=add],.side-link.active')?.focus();});});
render();
