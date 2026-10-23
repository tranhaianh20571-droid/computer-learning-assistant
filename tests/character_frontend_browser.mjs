import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {mockBuiltFrontend} from './frontend_dist_mock.mjs';

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+'/playwright-core');
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output='.backup/mentor-browser';await mkdir(output,{recursive:true});
const portrait=await readFile('文档/ui/板书与人物/assets/mentor-neutral.png');
const checks=[];
const profile={name:'知微',identity:'数学导师',personality:'沉稳、温柔',background:'喜欢把复杂关系写清楚。',appearance:'黑白衣装',tags:['温柔','清晰'],voice_id:'',voice_config_id:'',media_ids:['image'],base_asset_id:'image'};
const card={card_id:'synthetic-card',revision:1,published_revision:1,editable:true,collected:false,profile,assets:[{asset_id:'image',kind:'image',scene:'neutral',confirmed:true,url:'/api/cards/synthetic-card/assets/image'}]};
// Old published cards retain their parts, but only the complete portrait may render.
profile.pointing_rig={body_asset_id:'body',upper_arm_asset_id:'upper',forearm_asset_id:'lower',
 reference_asset_id:'image',shoulder_x:.34,shoulder_y:.43,upper_length:.22,forearm_length:.54};
for(const asset_id of ['body','upper','lower']){
 profile.media_ids.unshift(asset_id);
 card.assets.unshift({asset_id,kind:'image',scene:'neutral',confirmed:true,url:`/api/cards/synthetic-card/assets/${asset_id}`});
}
const text='先观察这张表，分清输入和对应值。每一个键都对应一个值，同一个键只能出现一次。';
const blocks=[{id:'table',type:'table',title:'键与值的关系',columns:['键','值'],rows:[['姓名','知微'],['课程','数学'],['状态','讲解中']]},{id:'formula',type:'formula',title:'唯一映射',formula:'f(x)=y',format:'latex',plain_text:'f(x) = y'}];
const board={kind:'board',beats:[{id:'b',text,block_ids:['table','formula']}],presentation:{version:2,title:'字典 · 键与值',blocks}};
const workspace=()=>({conversation_id:'synthetic-lesson',kind:'lesson',course_id:'c',lesson_id:'l',generation:1,session_state:'active',lesson_title:'理解键值映射',server_managed:true,audio_mode:'text',tts_authorized:false,queued_tasks:[],last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{paused:true,visible_chars:Array.from(text).length}},steps:[{step_no:1,step_id:'step',title:'键值映射',text,status:'ready',text_version_no:1,board,board_scope:'stage1',audio_id:null,audio_ready:false,character:{card_id:card.card_id,revision:1,profile}}],followups:[]});
try{
 for(const width of [1440,768,390,320]){
  const context=await browser.newContext({viewport:{width,height:900},serviceWorkers:'block'}),page=await context.newPage();
  page.setDefaultTimeout(15000);const served=await mockBuiltFrontend(page),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://esm.sh/**/CascadiaCode-Regular.woff2',async r=>r.fulfill({contentType:'font/woff2',body:await readFile('frontend/node_modules/@excalidraw/excalidraw/dist/prod/fonts/Cascadia/CascadiaCode-Regular.woff2')}));
  let ws=workspace(),saved=structuredClone(card),followups=0;const configs=[];
  const json=(r,b,status=200)=>r.fulfill({status,contentType:'application/json',body:JSON.stringify(b)});
  await page.route('**/api/**',async r=>{
   const req=r.request(),u=new URL(req.url()),p=u.pathname,m=req.method();
   if(p.endsWith('/assets/image')||p.endsWith('/1/image'))return r.fulfill({contentType:'image/png',body:portrait});
   if(p==='/api/auth/me')return json(r,{user_id:'synthetic-owner',status:'approved',is_admin:false});
   if(p==='/api/cards'&&m==='POST'){saved={...saved,profile:req.postDataJSON()};return json(r,saved,201);}
   if(p==='/api/cards'||p==='/api/card-market')return json(r,{data:[saved]});
   if(p==='/api/mentor-preference')return json(r,{version:0,card_id:null,revision:null});
   if(p==='/api/character-voices')return json(r,{data:[]});
   if(p==='/api/character-expression')return json(r,{data:[],recommended_voice_id:null,voice_gender:'unknown',
    match_status:'no_compatible_voice',speaking_style:'表达沉稳，耐心拆解。'});
   if(p==='/api/capabilities/configs'&&m==='POST'){const body=req.postDataJSON();configs.push({...body,config_id:`synthetic-video-${configs.length}`,owner_scope:'user',credential_mask:'****',config_version:1,is_active:true,capability_status:{},credentials:undefined});return json(r,configs.at(-1),201);}
   if(p==='/api/capabilities/configs')return json(r,{data:configs});
   if(p==='/api/capabilities/model-roles')return json(r,{data:[]});
   if(p==='/api/capabilities/reasoning/describe')return json(r,{schema_version:1,rules_version:'synthetic',source:'unknown',supported_modes:['default'],control_kind:'unknown',effort_values:[],budget:null,summary_supported:false});
   if(p==='/api/connectors'||p==='/api/materials'||p==='/api/navigation')return json(r,{data:[]});
   if(p.endsWith('/media-jobs'))return json(r,{data:[]});
   if(p==='/api/cards/synthetic-card'&&m==='PATCH'){const b=req.postDataJSON();saved={...saved,revision:saved.revision+1,profile:b.profile};return json(r,saved);}
   if(p==='/api/cards/synthetic-card')return json(r,saved);
   if(p.endsWith('/publish'))return json(r,saved);
   if(p.endsWith('/collect'))return json(r,{collected:true});
   if(p.endsWith('/character-assets/1'))return json(r,{data:card.assets.map(asset=>({...asset,
    url:`/api/teaching/synthetic-lesson/character-assets/1/${asset.asset_id}`}))});
   if(p==='/api/teaching/synthetic-lesson')return json(r,ws);
   if(p.endsWith('/checkpoint')){ws.last_checkpoint={...req.postDataJSON(),presentation:req.postDataJSON().presentation};return json(r,{saved:true});}
   if(p.endsWith('/presentation'))return json(r,{saved:true});
   if(p.endsWith('/followup')){followups++;ws.session_state='awaiting_resume';ws.generation=2;ws.followups=[{generation:2,role:'user',content:req.postDataJSON().content},{generation:2,role:'assistant',content:'因为键唯一，才能准确找到对应值。'}];return json(r,{task_id:'synthetic-answer',deduped:false});}
   if(p.endsWith('/run'))return json(r,{status:'succeeded'});
   if(p.endsWith('/content-events'))return r.fulfill({contentType:'text/event-stream',body:'event: done\ndata: {"event":"done"}\n\n'});
   if(p.endsWith('/resume')){ws.session_state='active';return json(r,{resumed:true});}
   throw new Error(`Unexpected API ${m} ${p}`);
  });
  await page.goto(served.origin+'/#/lecture/synthetic-lesson');
  await page.locator('.teaching-board-canvas').waitFor();
  await page.waitForFunction(()=>Number(document.querySelector('.teaching-board-canvas')?.dataset.boardElements)>8);
  await page.waitForFunction(()=>document.querySelector('.classroom-character img')?.getAttribute('src')?.endsWith('/1/image'));
  assert.equal(await page.locator('.character-rig').count(),0);
  assert.equal(await page.locator('.classroom-character img').count(),1);
  await page.locator('.classroom-character img').waitFor();
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('canvas')).some(canvas=>canvas.width>0));
  await page.evaluate(()=>document.fonts.ready);
  const metrics=await page.evaluate(()=>{const b=document.querySelector('.composer2').getBoundingClientRect();return {body:document.documentElement.scrollHeight,inner:innerHeight,width:document.documentElement.scrollWidth,innerWidth,composer:b.bottom,canvas:document.querySelector('canvas')?.width};});
  await page.screenshot({path:`${output}/classroom-${width}.png`});
  assert(metrics.body<=metrics.inner+2,JSON.stringify(metrics));assert(metrics.width<=metrics.innerWidth+2,JSON.stringify(metrics));assert(metrics.composer<=metrics.inner+1,JSON.stringify(metrics));assert(metrics.canvas>0);
  const varied=await page.evaluate(()=>Array.from(document.querySelectorAll('canvas')).some(canvas=>{const ctx=canvas.getContext('2d');if(!ctx)return false;const data=ctx.getImageData(0,0,canvas.width,canvas.height).data;const colors=new Set();for(let i=0;i<data.length;i+=160)colors.add(`${data[i]},${data[i+1]},${data[i+2]}`);return colors.size>12;}));assert(varied,'board canvas must contain visible drawing');
  await page.getByRole('button',{name:'继续讲解',exact:true}).click();
  await page.getByRole('button',{name:'停止讲解',exact:true}).click();
  await page.getByLabel('课程追问', {exact:true}).fill('为什么键要唯一？');
  await page.getByRole('button',{name:'发送追问',exact:true}).click();
  await page.getByRole('button',{name:'继续讲解',exact:true}).waitFor();assert.equal(followups,1);
  assert.equal(ws.session_state,'awaiting_resume');
  await page.getByRole('button',{name:'继续讲解',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.lecture-play-control')?.getAttribute('aria-label')==='继续讲解',{},{timeout:45000});
  await page.getByRole('button',{name:'继续讲解',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.lecture-play-control')?.getAttribute('aria-label')==='停止讲解');
  assert.equal(ws.session_state,'active');
  checks.push(`classroom-${width}: viewport, canvas, stop, ask, explicit resume`);
  if(width===1440){
   await page.goto(served.origin+'/#/card/synthetic-card');await page.getByLabel('名字',{exact:true}).fill('知微老师');
   await page.getByRole('button',{name:'发布到市集',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('.character-header h1')?.textContent==='知微老师');assert.equal(saved.profile.name,'知微老师');assert(saved.revision>1);
   assert.equal(await page.getByLabel('音色',{exact:true}).count(),1);assert.equal(await page.getByText('语速',{exact:true}).count(),0);
   await page.screenshot({path:`${output}/card-1440.png`});
   await page.goto(served.origin+'/#/card-market');await page.getByRole('heading',{name:'卡片市集',exact:true}).waitFor();
   await page.getByRole('button',{name:'收录人物',exact:true}).click();checks.push('card: save before publish, one voice, market collect');
   await page.goto(served.origin+'/#/capabilities?section=media');
   const video=page.getByRole('region',{name:'人物视频模型配置',exact:true});
   await video.getByLabel('服务地址',{exact:true}).fill('https://video.synthetic.test');
   await video.getByLabel('API Key',{exact:true}).fill('synthetic-video-key');
   await video.getByRole('button',{name:/保存/}).click();
   await page.waitForFunction(()=>document.querySelector('.provider-connection[aria-label="人物视频模型配置"] [role="status"]')?.textContent.includes('已保存'));
   assert.equal(configs[0].protocol,'minimax_video');assert.equal(configs[0].endpoint,'https://video.synthetic.test');
   await video.locator('select').selectOption('seedance');
   await video.getByLabel('服务地址',{exact:true}).fill('https://ark.synthetic.test/api/v3');
   await video.getByLabel('模型',{exact:true}).fill('synthetic-seedance');
   await video.getByLabel('API Key',{exact:true}).fill('synthetic-video-key');
   await video.getByRole('button',{name:/保存/}).click();
   await page.waitForFunction(()=>document.querySelector('.provider-connection[aria-label="人物视频模型配置"] [role="status"]')?.textContent.includes('已保存'));
   assert.equal(configs[1].protocol,'seedance');
   assert(!(await page.locator('body').innerText()).includes('synthetic-video-key'));
   checks.push('video: custom URL/key H3 and Seedance settings');
  }
  assert.deepEqual(errors,[]);assert.deepEqual(served.unexpected,[]);await context.close();
 }
 await writeFile(`${output}/results.json`,JSON.stringify({checks},null,2));console.log(JSON.stringify({checks}));
}finally{await browser.close();}
