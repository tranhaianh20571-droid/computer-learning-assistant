import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+'/playwright-core');
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const page=await browser.newPage({viewport:{width:1440,height:1000}}),checks=[],errors=[],requests=[];
const base=process.env.TEST_FRONTEND||'http://127.0.0.1:5173';
const common={scenario:'self',unit_count:1,lesson_total:1,lesson_completed:0,course_count:2,course_completed:0,search_titles:[],history_titles:[]};
let items=[{...common,id:'synthetic-route',kind:'route',title:'合成Python合集',state:'in_progress',completed:false},
 {...common,id:'synthetic-course',kind:'course',title:'合成独立课程',state:'idle',completed:false},
 {...common,id:'synthetic-completed',kind:'course',title:'合成完成课程',state:'done',completed:true},
 {...common,id:'synthetic-pending',kind:'course',title:'合成待确认课程',state:'preparing',completed:false}];
let failDelete=true,releaseDelete=null,heldDelete=false,planningPayload=null;
page.on('pageerror',e=>errors.push(e.message));
page.on('dialog',()=>{throw new Error('Native confirm is forbidden');});
const png=process.env.TEST_COVER_IMAGE ? await readFile(process.env.TEST_COVER_IMAGE) : Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jHdwAAAAASUVORK5CYII=','base64');
const screenshot = async name => {
 if(process.env.TEST_SCREENSHOT_DIR) {
  await page.evaluate(()=>document.fonts.ready);
  await page.waitForFunction(()=>innerWidth>=1024 || document.querySelector('#side')?.getBoundingClientRect().right<=1);
  await page.screenshot({path:process.env.TEST_SCREENSHOT_DIR+'/'+name+'.png',fullPage:true});
 }
};
await page.route('**/api/**',async route=>{
 const req=route.request(),url=new URL(req.url()),path=url.pathname,method=req.method();requests.push({path,method});
 let body={data:[]},status=200;
 if(path.endsWith('/auth/guest')||path.endsWith('/auth/me'))body={user_id:'synthetic-owner',status:'approved',is_admin:false};
 else if(path==='/api/planning/library')body={data:items};
 else if(method==='DELETE'){
  if(!heldDelete){heldDelete=true;await new Promise(resolve=>{releaseDelete=resolve;});}
  if(failDelete){failDelete=false;status=503;body={detail:{message:'合成删除失败，请重试'}};}
  else if(path==='/api/planning/routes/synthetic-route'){
   // ADR-038 cascade: the route's current version organizes synthetic-pending, so deleting the
   // route deletes that course too; an independent course is never a current member.
   items=items.filter(c=>c.id!=='synthetic-route'&&c.id!=='synthetic-pending');
   body={deleted:true,courses_deleted:1,course_ids:['synthetic-pending']};
  }
  else{items=items.filter(c=>!path.endsWith('/'+c.id));body={deleted:true};}
 }
 else if(path.endsWith('/overview')&&path.includes('/routes/'))body={title:'合成Python合集',scenario:'self',is_current:!url.searchParams.has('version_id'),version_no:1,current_version_id:'synthetic-v1',course_count:1,course_completed:0,history:[],stages:[{stage_id:'synthetic-stage',title:'合成阶段',course_count:1,courses:[{course_id:'synthetic-pending',node_id:'synthetic-node',title:'合成待确认课程',order_index:0,outline_confirmed:false,lesson_completed:0,lesson_total:1}]}]};
 else if(path.endsWith('/overview')&&path.includes('/courses/'))body={title:'合成独立课程',scenario:'self',outline_confirmed:true,historical_outline:true,version_no:1,contexts:[],context:null,progress:{lesson_total:1,lesson_completed:0},units:[],teaching_unlocked:true};
 else if(path.endsWith('/cover'))body={asset_id:'synthetic-cover',status:'succeeded',method:'image',image_url:'/api/planning/covers/synthetic-cover/image'};
 else if(path.endsWith('/image')){await route.fulfill({status:200,contentType:'image/png',body:png});return;}
 else if(path==='/api/planning/tasks'&&method==='POST'){planningPayload=req.postDataJSON();body={task_id:'synthetic-plan-task'};}
 else if(path.includes('/first-preparation'))body={status:'not_generated'};
 await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
});
try{
 await page.goto(base+'/#/courses');await page.getByRole('heading',{name:'合成Python合集',exact:true}).waitFor();
 const states=await page.locator('.library-state').evaluateAll(xs=>xs.map(x=>({cls:x.className,animation:getComputedStyle(x.querySelector('i')).animationName})));
 assert.equal(states.length,4);assert(states.some(x=>x.cls.includes('is-active')));assert(states.some(x=>x.cls.includes('is-done')));assert(states.some(x=>x.cls.includes('is-idle')));assert(states.some(x=>x.cls.includes('is-preparing')));assert(states.every(x=>x.animation==='none'));assert.equal(await page.locator('.ccard .ver-draft').count(),0);checks.push('independent_static_state_patterns');await screenshot('library-desktop');
 const card=()=>page.locator('.ccard').filter({has:page.getByRole('heading',{name:'合成Python合集',exact:true})});
 await card().getByRole('button',{name:'删除合集',exact:true}).click();
 let dialog=page.getByRole('dialog');await dialog.getByText('将删除该合集当前编排中的全部课程，连同其大纲、讲次和学习记录；已移出的课程与共享资料会保留。此操作无法撤销。',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'取消',exact:true}).click();assert.equal(requests.filter(x=>x.method==='DELETE').length,0);checks.push('cancel_delete_sends_no_request');
 await card().getByRole('button',{name:'删除合集',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'确认删除',exact:true}).click();
 await page.getByRole('dialog').getByRole('button',{name:'正在删除…',exact:true}).waitFor();
 assert(await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).isDisabled());assert.equal(requests.filter(x=>x.method==='DELETE').length,1);releaseDelete();
 await page.getByRole('dialog').getByRole('alert').waitFor();assert.equal(await card().count(),1);checks.push('busy_guard_and_failure_preserves_card');
 await page.getByRole('dialog').getByRole('button',{name:'确认删除',exact:true}).click();await card().waitFor({state:'hidden'});
 // ADR-038 cascade: the route's current member disappears with the route; the independent course stays.
 await page.getByRole('heading',{name:'合成待确认课程',exact:true}).waitFor({state:'hidden'});
 await page.getByRole('heading',{name:'合成独立课程',exact:true}).waitFor();assert(requests.some(x=>x.path==='/api/planning/routes/synthetic-route'&&x.method==='DELETE'));checks.push('route_delete_cascades_current_member_and_refreshes');
 const courseCard=page.locator('.ccard').filter({has:page.getByRole('heading',{name:'合成独立课程',exact:true})});
 await courseCard.getByRole('button',{name:'删除课程',exact:true}).click();await page.getByRole('dialog').getByText('将删除这门课程的大纲、讲次和相关学习产物，并从所属合集中移除。此操作无法撤销。',{exact:true}).waitFor();
 await page.getByRole('dialog').getByRole('button',{name:'确认删除',exact:true}).click();await courseCard.waitFor({state:'hidden'});assert(requests.some(x=>x.path==='/api/planning/courses/synthetic-course'&&x.method==='DELETE'));checks.push('course_delete_uses_course_endpoint');
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));checks.push('mobile_library_no_overflow');await screenshot('library-mobile');
 await page.setViewportSize({width:1440,height:1000});await page.goto(base+'/#/route/synthetic-route');await page.locator('.route-ink-cover img').waitFor();await page.waitForFunction(()=>document.querySelector('.route-ink-cover img')?.naturalWidth>0);await screenshot('overview-desktop');
 assert.equal(await page.getByRole('button',{name:/查看导图|生成水墨|使用图片模型|生成封面/}).count(),0);assert.equal(await page.locator('.phead .ver').count(),0);assert(!requests.some(x=>x.path.includes('/scene')));assert(!requests.some(x=>x.method==='POST'&&x.path.endsWith('/cover')));
 const bounds=await page.locator('.route-ink-cover').evaluate(el=>{const box=el.getBoundingClientRect(),parent=el.parentElement.getBoundingClientRect();return{width:box.width,height:box.height,center:box.x+box.width/2,parentCenter:parent.x+parent.width/2,fit:getComputedStyle(el.querySelector('img')).objectFit};});
 assert(bounds.width>=480&&bounds.width<=560);assert(Math.abs(bounds.center-bounds.parentCenter)<1);assert(Math.abs(bounds.width/bounds.height-4/3)<.02);assert.equal(bounds.fit,'contain');checks.push('route_cover_centered_and_tools_hidden_without_generation');
 await page.getByRole('button',{name:'生成课程大纲',exact:true}).click();await page.waitForFunction(()=>location.hash.includes('/planning'));
 assert.equal(planningPayload.task_kind,'outline_generate');assert.equal(planningPayload.target_course_id,'synthetic-pending');
 // ADR-034 automatic mode: the outline action never authorizes an image request itself; the cover is
 // enqueued server-side when the outline draft is confirmed.
 assert.equal(planningPayload.cover_auto_generate,undefined);checks.push('outline_action_keeps_cover_authorization_server_side');
 await page.goto(base+'/#/route/synthetic-route?version_id=synthetic-old');await page.getByText('历史内容',{exact:true}).waitFor();checks.push('historical_route_hint_preserved');
 await page.goto(base+'/#/course/synthetic-completed');await page.getByText('正在查看历史课程内容',{exact:true}).waitFor();assert.equal(await page.getByText('大纲版本',{exact:true}).count(),0);assert.equal(await page.getByRole('button',{name:/生成水墨|使用图片模型/}).count(),0);checks.push('course_overview_tools_hidden_history_preserved');
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));checks.push('mobile_overview_no_overflow');await screenshot('overview-mobile');
 assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,synthetic:true,pageErrors:errors}));
}catch(e){console.log(JSON.stringify({checks,pageErrors:errors}));throw e;}finally{await browser.close();}
