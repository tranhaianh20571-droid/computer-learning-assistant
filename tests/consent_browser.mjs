import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import path from 'node:path';
const require=createRequire(path.resolve(process.env.PW_ROOT||'frontend/node_modules','package.json'));
const {chromium}=require('playwright-core');
const browser=await chromium.launch({executablePath:process.env.TEST_BROWSER,headless:true});
const page=await browser.newPage({viewport:{width:1280,height:900}}),checks=[],errors=[],posts=[];
page.on('pageerror',e=>errors.push(e.message));
const service={config_id:'synthetic-content',config_version:4,model_name:'Synthetic model',protocol:'openai',service_endpoint:'https://synthetic.invalid/'+('very-long-endpoint/'.repeat(12))};
let configured=true,stale=false;
await page.route('**/api/**',async route=>{
 const request=route.request(),url=new URL(request.url());let body={},status=200;
 if(request.method()==='POST' && /\/start$|\/prewarm$|\/recover$|authorization-confirm|\/cover(?:\/automatic)?$/.test(url.pathname))posts.push({path:url.pathname,body:request.postData()?request.postDataJSON():null});
 if(url.pathname==='/api/auth/me')body={user_id:'synthetic-user',status:'approved',role:'user'};
 else if(/\/start$|\/prewarm$|\/recover$/.test(url.pathname)){
  if(stale){status=409;body={error_code:'disclosure_revoked',message:'revoked'};}
  else if(!configured){status=409;body={error_code:'config_not_found',message:'model configuration required'};}
  else body={conversation_id:'synthetic-conversation',task_id:'synthetic-task',status:'queued'};
 }
 else if(url.pathname.endsWith('/cover/preflight'))body={title:'Synthetic route',title_hash:'t'.repeat(64),config:configured?service:null,configs:configured?[service,{...service,config_id:'synthetic-second',config_version:9}]:[],course_topics:[{title:'Confirmed course',title_hash:'c'.repeat(64)}]};
 else if(url.pathname.endsWith('/cover'))body={status:'succeeded',method:'procedural',requested_method:'image',actual_method:'procedural',fallback_reason:'MODEL_TIMEOUT'};
 else if(url.pathname.endsWith('/cover/automatic'))body={status:'queued'};
 await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
});
const call=async(method,args)=>page.evaluate(async({method,args})=>{
 const {api}=await import('/src/api.ts');window.consentResult=undefined;
 api[method](...args).then(value=>window.consentResult={value}).catch(e=>window.consentResult={error:e.code});
},{method,args});
const result=async()=>{await page.waitForFunction(()=>window.consentResult!==undefined);return page.evaluate(()=>window.consentResult);};
try{
 await page.goto((process.env.TEST_FRONTEND||'http://127.0.0.1:5173')+'/#/courses');
 const dialog=page.getByRole('dialog');
 for(const [method,args,expected] of [
  ['startLessonV2',['course','lesson'],'/api/courses/course/lessons/lesson/start'],
  ['prewarmLesson',['course','lesson'],'/api/courses/course/lessons/lesson/prewarm'],
  ['recoverLesson',['course','lesson','conv'],'/api/teaching/conv/recover']]){
  await call(method,args);assert.equal((await result()).value.status,'queued');assert.equal(await dialog.count(),0);assert.equal(posts.at(-1).path,expected);assert.equal(posts.at(-1).body,null);
 }
 assert.equal(posts.filter(post=>post.path.endsWith('/authorization-confirm')).length,0);checks.push('teaching_direct_start_prewarm_recover_without_consent');
 stale=true;const before=posts.length;await call('recoverLesson',['course','lesson','conv']);assert.equal((await result()).error,'disclosure_revoked');assert.equal(posts.length,before+1);assert.equal(await dialog.count(),0);checks.push('revoked_teaching_no_silent_retry');stale=false;
 configured=false;await call('startLessonV2',['course','lesson']);assert.equal((await result()).error,'config_not_found');assert.equal(await dialog.count(),0);checks.push('unconfigured_model_returns_error_without_dialog');configured=true;
 await call('authorizeCover',['route','route']);await dialog.waitFor();assert.equal(await dialog.locator('input:checked').count(),0);await dialog.locator('input').first().check();await dialog.locator('select').selectOption('synthetic-second');assert.equal(await dialog.locator('input:checked').count(),0);await dialog.locator('input').first().check();assert.equal(await dialog.locator('input').count(),1);await page.setViewportSize({width:390,height:844});assert(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1));await dialog.getByRole('button',{name:'确认并继续',exact:true}).click();await result();const cover=posts.at(-1).body;assert.equal(cover.config_id,'synthetic-second');assert.equal(cover.config_version,9);assert.equal(cover.authorization_scope,'exact_target');assert.deepEqual(cover.course_topic_hashes,[]);checks.push('cover_selected_version_exact_target_mobile');
 configured=false;await call('authorizeCover',['course','local']);await dialog.waitFor();assert.equal(await dialog.locator('input:checked').count(),0);await dialog.getByRole('button',{name:'生成本地封面',exact:true}).click();await result();assert.equal(posts.at(-1).path,'/api/planning/course/local/cover');assert.equal(posts.at(-1).body.method,'procedural');assert.equal(posts.at(-1).body.external_authorized,undefined);checks.push('no_image_config_explicit_local_cover_no_external_grant');
 await page.evaluate(async()=>{const React=await import('/node_modules/.vite/deps/react.js');const ReactDOM=await import('/node_modules/.vite/deps/react-dom_client.js');const createRoot=ReactDOM.createRoot || ReactDOM.default?.createRoot;const {default:CourseCover}=await import('/src/ui/CourseCover.tsx');const host=document.createElement('div');host.id='cover-regression';document.body.append(host);createRoot(host).render((React.createElement || React.default.createElement)(CourseCover,{kind:'course',id:'fallback',title:'Fallback course'}));});
 const fallback=page.locator('#cover-regression');await fallback.locator('[data-cover-state=fallback]').waitFor();assert.equal(await fallback.getByText(/图片模型封面|本地程序化封面|图片生成未完成/).count(),0);assert(await fallback.getByRole('button',{name:'生成封面',exact:true}).isVisible());await fallback.getByRole('button',{name:'生成封面',exact:true}).click();await dialog.waitFor();await dialog.getByRole('button',{name:'取消',exact:true}).click();checks.push('cover_method_labels_hidden_regenerate_available');
 assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,pageErrors:errors,synthetic:true}));
}finally{await browser.close();}
