import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {mockBuiltFrontend} from './frontend_dist_mock.mjs';

// Bounded synthetic production-build checks; every API/asset is intercepted, no supplier/server.
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+'/playwright-core');
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=path.resolve(process.env.TEST_OUTPUT||'.backup/reasoning-browser');
await mkdir(output,{recursive:true});
const checks=[],errors=[];
const json=(route,body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
const gate=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const unknown={schema_version:1,rules_version:'synthetic-1',source:'unknown',supported_modes:['default'],effort_values:[],budget:null,summary_supported:false,control_kind:'unknown'};
const effort={...unknown,source:'rule',supported_modes:['default','effort'],effort_values:['low','high','max'],summary_supported:true,control_kind:'thinking+reasoning_effort',endpoint_scope:'compatible_unverified'};
const budget={...effort,supported_modes:['default','budget','auto','off'],effort_values:[],budget:{min:1024,max:8192},control_kind:'thinkingBudget'};
const manualProfiles={
 openai:{supported_modes:['default','effort','off'],effort_values:['none','minimal','low','medium','high','xhigh','max'],budget:null,summary_supported:false,control_kind:'effort'},
 anthropic:{supported_modes:['default','effort','budget','auto','off'],effort_values:['low','medium','high','max'],budget:{min:1024,max:32000},summary_supported:true,control_kind:'manual_anthropic'},
 gemini:{supported_modes:['default','effort','budget','auto','off'],effort_values:['minimal','low','medium','high'],budget:{min:128,max:32768},summary_supported:true,control_kind:'manual_gemini'}
};
const chooseCaps=(model,protocol='openai')=>model.includes('budget')?budget:model.includes('effort')?effort:{...unknown,manual_controls:manualProfiles[protocol]};
const defaults={mode:'default',show_summary:true};
const fixture=(body={})=>({config_id:'synthetic-reasoning-config',owner_scope:'user',kind:'content_model',protocol:'openai',endpoint:'https://model.synthetic.test/v1',model_name:'synthetic-effort',credential_mask:'****',capability_status:{},config_version:1,is_active:true,reasoning_settings:defaults,reasoning_capabilities:effort,...body});
const task=(id='synthetic-process-task',generation=1)=>({task_id:id,generation,title:'合成前端开发课程',task_status:'running',confirmed:false,target_id:'',target_kind:'route',wait_state:null});
const processFixture=(id='synthetic-process-task',generation=1,sequence=3)=>({schema_version:1,task_id:id,generation,sequence,task_status:'running',stage:'generate',started_at:new Date(Date.now()-45000).toISOString(),updated_at:new Date().toISOString(),elapsed_ms:45000,steps:[{key:'intake',label:'整理学习目标',status:'succeeded'},{key:'search',label:'搜索参考资料',status:'succeeded'},{key:'generate',label:'构建课程路线',status:'running'}],search:{status:'succeeded',query:'合成前端开发学习资料',result_count:4,sources:[{title:'JavaScript 合成教程',domain:'developer.synthetic.test',url:'https://developer.synthetic.test/javascript',used:true},{title:'React 合成资料',domain:'react.synthetic.test',url:'https://react.synthetic.test/learn',used:true},{title:'<img src=x onerror=alert(1)>',domain:'unsafe.synthetic.test',url:'javascript:alert(1)',used:false},{title:'拒绝带凭据的来源',domain:'credential.synthetic.test',url:'https://user:password@credential.synthetic.test/guide',used:false}]},thinking:{show_summary:true,available:true,text:'先安排基础，再加入合成实践。\n<script>synthetic-thought-marker</script>'}});
const context=await browser.newContext({viewport:{width:1440,height:1050},serviceWorkers:'block',reducedMotion:'reduce'});
const page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
const served=await mockBuiltFrontend(page);
let config=fixture(),currentTask=task(),snapshot=processFixture(),processFailure=false,describeFailure=false;
const requests=[],saved=[];let pendingDescribe=null,pendingProcess=null;
await page.route('**/api/**',async route=>{
 const request=route.request(),url=new URL(request.url()),method=request.method();requests.push({path:url.pathname,method});
 if(url.pathname==='/api/auth/me'||url.pathname==='/api/auth/guest')return json(route,{user_id:'synthetic-reasoning-owner',email:'reasoning@example.test',is_admin:false,status:'approved'});
 if(url.pathname==='/api/capabilities/reasoning/describe'){
  const body=request.postDataJSON();assert(!('credentials' in body));assert(!('api_key' in body));
  if(pendingDescribe&&body.model_name===pendingDescribe.model){pendingDescribe.arrived.resolve();await pendingDescribe.release.promise;}
  if(describeFailure)return json(route,{detail:{error_code:'ERROR',message:'synthetic-private-provider-raw'}},500);
  return json(route,chooseCaps(body.model_name,body.protocol)).catch(()=>{});
 }
 if(url.pathname==='/api/capabilities/configs'&&method==='GET')return json(route,{data:[config]});
 if(url.pathname==='/api/capabilities/configs'&&method==='POST'){
  const body=request.postDataJSON();saved.push(body);config=fixture({...body,reasoning_capabilities:chooseCaps(body.model_name,body.protocol)});return json(route,config,201);
 }
 if(url.pathname==='/api/capabilities/configs/synthetic-reasoning-config'&&method==='PATCH'){
  const body=request.postDataJSON();saved.push(body);config=fixture({...config,...body,config_version:config.config_version+1});return json(route,config);
 }
 if(url.pathname==='/api/capabilities/configs/synthetic-reasoning-config/reasoning/test'){
  assert.equal(request.postDataJSON().config_version,config.config_version);return json(route,{evidence:'request_accepted',config_version:config.config_version});
 }
 if(url.pathname==='/api/connectors'||url.pathname==='/api/materials')return json(route,{data:[]});
 if(url.pathname==='/api/planning/current')return json(route,{planning:currentTask});
 if(url.pathname.endsWith('/process')){
  if(pendingProcess&&url.pathname.includes(pendingProcess.id)){const held=pendingProcess;held.arrived.resolve();await held.release.promise;return json(route,{process:held.snapshot}).catch(()=>{});}
  if(processFailure)return json(route,{detail:{error_code:'ERROR',message:'synthetic-private-provider-raw'}},503);
  return json(route,{process:snapshot});
 }
 if(url.pathname===`/api/planning/tasks/${currentTask.task_id}`||url.pathname.endsWith('/resume'))return json(route,currentTask);
 throw Error(`Unexpected synthetic API ${method} ${url.pathname}`);
});
const form=()=>page.locator('.card').filter({has:page.getByRole('heading',{name:'个人内容模型',exact:true})});
const ready=()=>form().getByText('规则识别',{exact:true}).waitFor();
const screenshot=async name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
const noOverflow=()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1);
const navigate=route=>page.goto(`${served.origin}/#/${route}`);
const saveModel=async()=>{
 const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/capabilities/configs'&&r.request().method()==='POST');
 await form().getByLabel('API Key',{exact:true}).fill('synthetic-key');
 await form().getByRole('button',{name:'保存模型配置',exact:true}).click();await response;
 await page.getByText('模型配置已保存（凭据已加密）',{exact:true}).waitFor();
};
try {
 await navigate('capabilities');await form().waitFor();
 await form().getByLabel('服务地址',{exact:true}).fill('https://model.synthetic.test/v1');
 await form().getByLabel('模型名',{exact:true}).fill('synthetic-effort');await ready();
 await form().getByLabel('指定思考强度',{exact:true}).check();
 await form().getByRole('button',{name:'深入 high',exact:true}).click();
 assert.equal(await form().getByRole('button',{name:'深入 high',exact:true}).getAttribute('aria-pressed'),'true');
 assert.equal(await form().getByLabel('显示模型返回的思考内容').count(),0);
 await form().getByLabel('API Key',{exact:true}).fill('synthetic-key');
 await form().getByRole('button',{name:'保存模型配置',exact:true}).click();
 await page.getByText('模型配置已保存（凭据已加密）',{exact:true}).waitFor();
 assert.deepEqual(saved.at(-1).reasoning_settings,{mode:'effort',effort:'high',show_summary:false});
 checks.push('native_effort_create_and_independent_summary');
 await page.getByRole('button',{name:'编辑',exact:true}).click();
 const dialog=page.getByRole('dialog',{name:'编辑配置',exact:true});await dialog.waitFor();
 await dialog.getByText('规则识别',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'验证已保存组合',exact:true}).click();
 await dialog.getByText('已保存组合的请求被接受；这不保证档位参数实际生效。',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'最大 max',exact:true}).click();
 assert(await dialog.getByRole('button',{name:'验证已保存组合',exact:true}).isDisabled());
 await dialog.getByRole('button',{name:'保存修改',exact:true}).click();
 await page.getByText('配置已更新；能力将重新测试',{exact:true}).waitFor();
 assert.deepEqual(saved.at(-1).reasoning_settings,{mode:'effort',effort:'max',show_summary:false});checks.push('edit_roundtrip_and_saved_only_verification');
 await page.getByRole('button',{name:'编辑',exact:true}).click();await dialog.getByText('规则识别',{exact:true}).waitFor();
 describeFailure=true;await dialog.getByRole('button',{name:'重新检测档位',exact:true}).click();
 await dialog.getByText('能力说明暂不可用，当前设置已保留。请重新检测后再保存。',{exact:true}).waitFor();
 assert.equal(await dialog.getByRole('button',{name:'最大 max',exact:true}).getAttribute('aria-pressed'),'true');
 assert(await dialog.getByRole('button',{name:'保存修改',exact:true}).isDisabled());
 assert(!(await dialog.innerText()).includes('synthetic-private-provider-raw'));await screenshot('settings-detection-error');
 describeFailure=false;await dialog.getByRole('button',{name:'重新检测档位',exact:true}).click();await dialog.getByText('规则识别',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'取消',exact:true}).click();checks.push('saved_effort_preserved_when_detection_fails');
 await page.getByRole('navigation',{name:'设置分类'}).getByRole('button',{name:'内容模型',exact:true}).click();
 await form().getByLabel('模型名',{exact:true}).fill('synthetic-budget');await ready();
 await form().getByLabel('指定思考预算',{exact:true}).check();
 const input=form().getByRole('spinbutton');await input.fill('512');
 assert.equal(await input.getAttribute('aria-invalid'),'true');assert(await form().getByRole('button',{name:'保存模型配置',exact:true}).isDisabled());
 await input.fill('4096');assert.equal(await input.getAttribute('aria-invalid'),'false');await screenshot('settings-budget');
 await form().getByLabel('自动预算',{exact:true}).check();await form().getByLabel('关闭思考',{exact:true}).check();
 await form().getByLabel('供应商默认',{exact:true}).check();checks.push('budget_range_and_distinct_auto_off_default');
 pendingDescribe={model:'synthetic-effort-late',arrived:gate(),release:gate()};
 await form().getByLabel('模型名',{exact:true}).fill(pendingDescribe.model);await pendingDescribe.arrived.promise;
 await form().getByLabel('模型名',{exact:true}).fill('synthetic-unknown');
 await form().getByText('能力未知',{exact:true}).waitFor();pendingDescribe.release.resolve();pendingDescribe=null;
 await page.waitForTimeout(400);
 assert.equal(await form().getByLabel('指定思考强度',{exact:true}).count(),0);assert(await form().getByLabel('供应商默认',{exact:true}).isChecked());checks.push('late_descriptor_ignored_unknown_resets_mode');
 describeFailure=true;await form().getByRole('button',{name:'重新检测档位',exact:true}).click();
 await form().getByText('能力说明暂不可用，当前设置已保留。请重新检测后再保存。',{exact:true}).waitFor();
 assert(await form().getByRole('button',{name:'保存模型配置',exact:true}).isDisabled());
 assert(!(await page.locator('body').innerText()).includes('synthetic-private-provider-raw'));describeFailure=false;
 for(const protocol of ['openai','anthropic','gemini']){
  await form().getByRole('combobox').selectOption(protocol);
  await form().getByLabel('服务地址',{exact:true}).fill('https://model.synthetic.test/v1');
  await form().getByLabel('模型名',{exact:true}).fill(`synthetic-unlisted-${protocol}`);
  const manualToggle=form().getByLabel('手动指定思考参数',{exact:false});
  await manualToggle.check();await form().getByText('手动配置 · 未验证',{exact:true}).waitFor();
  await form().getByLabel('指定思考强度',{exact:true}).check();
  await form().getByRole('button',{name:'深入 high',exact:true}).click();
  assert.equal(await form().getByRole('button',{name:'最大 max',exact:true}).count(),protocol==='gemini'?0:1);
  assert.equal(await form().getByLabel('指定思考预算',{exact:true}).count(),protocol==='openai'?0:1);
  for(const width of [320,390]){await page.setViewportSize({width,height:900});assert(await noOverflow(),`manual effort overflow ${protocol} ${width}`);await screenshot(`manual-${protocol}-effort-${width}`);}
  await page.setViewportSize({width:1440,height:1050});
  await saveModel();
  assert.deepEqual(saved.at(-1).reasoning_settings,{mode:'effort',effort:'high',show_summary:false,manual:true});
  await page.getByRole('button',{name:'编辑',exact:true}).click();await dialog.getByText('手动配置 · 未验证',{exact:true}).waitFor();
  assert(await dialog.getByLabel('手动指定思考参数',{exact:false}).isChecked());
  assert.equal(await dialog.getByRole('button',{name:'深入 high',exact:true}).getAttribute('aria-pressed'),'true');
  await dialog.getByRole('button',{name:'验证已保存组合',exact:true}).click();
  await dialog.getByText('已保存组合的请求被接受；这不保证档位参数实际生效。',{exact:true}).waitFor();
  assert.equal(await dialog.getByText('规则识别',{exact:true}).count(),0);
  await dialog.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('navigation',{name:'设置分类'}).getByRole('button',{name:'内容模型',exact:true}).click();
  if(protocol!=='openai'){
   await form().getByLabel('指定思考预算',{exact:true}).check();await form().getByRole('spinbutton').fill('2048');
   await saveModel();
   assert.deepEqual(saved.at(-1).reasoning_settings,{mode:'budget',budget_tokens:2048,show_summary:false,manual:true});
   await form().getByLabel('自动预算',{exact:true}).check();assert(await form().getByLabel('自动预算',{exact:true}).isChecked());
  }
  await form().getByLabel('关闭思考',{exact:true}).check();
  await page.setViewportSize({width:390,height:900});assert(await noOverflow());await screenshot(`manual-${protocol}-390`);
  await page.setViewportSize({width:1440,height:1050});
  await manualToggle.uncheck();assert.equal(await form().getByLabel('指定思考强度',{exact:true}).count(),0);
  assert(await form().getByLabel('供应商默认',{exact:true}).isChecked());
  checks.push(`manual_${protocol}_create_edit_verify_budget_off_mobile`);
 }
 await form().getByLabel('手动指定思考参数',{exact:false}).check();await form().getByLabel('指定思考强度',{exact:true}).check();
 await form().getByRole('combobox').selectOption('openai');await form().getByText('能力未知',{exact:true}).waitFor();
 assert(await form().getByLabel('供应商默认',{exact:true}).isChecked());assert(!(await form().getByLabel('手动指定思考参数',{exact:false}).isChecked()));
 checks.push('manual_reset_on_protocol_change');
 await form().getByLabel('模型名',{exact:true}).fill('synthetic-effort');await ready();
 await form().getByLabel('指定思考强度',{exact:true}).check();await form().getByText('参数依据与检测',{exact:true}).click();
 await form().getByText(/兼容网关：根据模型名推断/).waitFor();checks.push('detection_failure_safe_retry_and_gateway_evidence');
 for(const width of [1440,768,390,320]){await page.setViewportSize({width,height:1050});assert(await noOverflow(),`settings overflow ${width}`);await screenshot(`settings-${width}`);assert(await form().getByRole('button',{name:'重新检测档位',exact:true}).isVisible());}
 await page.setViewportSize({width:390,height:900});await page.keyboard.press('Tab');await form().getByRole('button',{name:'重新检测档位',exact:true}).focus();assert(await form().getByRole('button',{name:'重新检测档位',exact:true}).evaluate(el=>el.matches(':focus-visible')&&getComputedStyle(el).outlineStyle==='solid'));await screenshot('settings-keyboard-focus');checks.push('settings_responsive320_390_768_1440_keyboard_focus');
 await page.setViewportSize({width:1440,height:1050});await navigate('planning');
 const panel=page.getByRole('region',{name:'规划生成过程',exact:true});await panel.getByText('构建课程路线',{exact:true}).waitFor();
 assert.equal(await panel.getByRole('button',{name:'展开 ⌄',exact:true}).getAttribute('aria-expanded'),'false');await screenshot('planning-folded-1440');
 await panel.getByRole('button',{name:'展开 ⌄',exact:true}).click();assert.equal(await panel.locator('.generation-source-list li').count(),2);
 await panel.getByRole('button',{name:'查看另 2 个来源',exact:true}).click();assert.equal(await panel.locator('.generation-source-list li').count(),4);
 assert.equal(await panel.locator('.generation-source-list a').count(),2);assert.equal(await panel.locator('.generation-source-list img').count(),0);
 assert.equal(await panel.locator('.generation-thinking').count(),0);
 assert.equal(await panel.getByText('synthetic-thought-marker',{exact:false}).count(),0);
 checks.push('snapshot_fold_sources_safe_links_and_no_thinking');
 for(const width of [1440,768,390,320]){await page.setViewportSize({width,height:1050});assert(await noOverflow(),`planning overflow ${width}`);await screenshot(`planning-${width}`);}
 const processCalls=requests.filter(r=>r.path.endsWith('/process')).length;snapshot={...snapshot,sequence:2,search:{...snapshot.search,query:'synthetic-regressed-query'}};
 await page.waitForTimeout(2200);assert(requests.filter(r=>r.path.endsWith('/process')).length>processCalls);assert.equal(await panel.getByText('synthetic-regressed-query',{exact:false}).count(),0);checks.push('monotonic_sequence_guard');
 snapshot={...processFixture(),sequence:4,thinking:{show_summary:false,available:false,text:''}};
 assert.equal(await panel.locator('.generation-thinking').count(),0);await screenshot('planning-summary-off');
 processFailure=true;await panel.getByText(/进度连接暂时中断/).waitFor();await screenshot('planning-connection-error');
 const resumes=requests.filter(r=>r.path.endsWith('/resume')).length;processFailure=false;snapshot={...processFixture(),sequence:5};
 await panel.getByRole('button',{name:'重新连接进度',exact:true}).click();await panel.getByRole('heading',{name:'搜索参考资料',exact:true}).waitFor();
 assert.equal(requests.filter(r=>r.path.endsWith('/resume')).length,resumes);checks.push('connection_retry_does_not_regenerate');
 snapshot={...processFixture(),task_status:'succeeded',sequence:6,steps:processFixture().steps.map(s=>({...s,status:'succeeded'}))};
 await panel.getByText('规划生成完成',{exact:true}).waitFor();await screenshot('planning-success');
 const stoppedCount=requests.filter(r=>r.path.endsWith('/process')).length;await page.waitForTimeout(2200);assert.equal(requests.filter(r=>r.path.endsWith('/process')).length,stoppedCount);checks.push('terminal_snapshot_stops_polling');
 await navigate('capabilities');currentTask=task('synthetic-new-task',2);snapshot=processFixture('synthetic-new-task',2);
 await navigate('planning');await panel.getByText('构建课程路线',{exact:true}).waitFor();await panel.getByRole('button',{name:'展开 ⌄',exact:true}).click();
 snapshot={...snapshot,thinking:{show_summary:true,available:false,text:''},search:{status:'skipped',result_count:0,sources:[]},sequence:4};
 await panel.getByText('本次未使用联网搜索。',{exact:true}).waitFor();assert.equal(await panel.locator('.generation-thinking').count(),0);await screenshot('planning-no-search-no-thinking');checks.push('empty_search_and_no_thinking_panel');
 snapshot={...snapshot,sequence:5,thinking:{show_summary:true,available:true,text:'合成思考内容已达到展示上限。',truncated:true}};
 assert.equal(await panel.locator('.generation-thinking').count(),0);checks.push('truncated_thinking_also_hidden');
 // A delayed old-task business snapshot is aborted when navigating away, then cannot leak to the new task.
 pendingProcess={id:'synthetic-new-task',arrived:gate(),release:gate(),snapshot:{...snapshot,sequence:8,thinking:{show_summary:true,available:true,text:'synthetic-old-task-secret'}}};
 await pendingProcess.arrived.promise;const old=pendingProcess;await navigate('capabilities');currentTask=task('synthetic-newer-task',3);snapshot=processFixture('synthetic-newer-task',3);pendingProcess=null;
 await navigate('planning');await panel.getByText('构建课程路线',{exact:true}).waitFor();await panel.getByRole('button',{name:'展开 ⌄',exact:true}).click();old.release.resolve();await page.waitForTimeout(200);
 assert(!(await panel.innerText()).includes('synthetic-old-task-secret'));checks.push('unmounted_old_task_response_isolated');
 snapshot={...snapshot,sequence:6,task_status:'failed',steps:snapshot.steps.map(s=>({...s,status:s.key==='generate'?'failed':'succeeded'}))};
 await panel.getByText('生成暂未完成',{exact:true}).waitFor();await panel.locator('.is-failed').getByText('失败',{exact:true}).waitFor();
 assert.equal(await panel.locator('.is-failed .generation-step-dot').innerText(),'!');
 assert(await panel.locator('.is-failed .generation-step-dot').evaluate(el=>getComputedStyle(el).color!==getComputedStyle(document.querySelector('.generation-progress-marker')).color));await screenshot('planning-failed');checks.push('failed_stage_visible');
 assert.deepEqual(errors,[]);assert.deepEqual(served.unexpected,[]);
 const result={synthetic:true,checks,pageErrors:errors,realProviderTested:false,realResumeSuiteExecuted:false};await writeFile(path.join(output,'results.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 // Separate clean presentation shots from hostile synthetic fixtures used above.
 await page.setViewportSize({width:1440,height:1350});await navigate('capabilities');
 await form().getByLabel('服务地址',{exact:true}).fill('https://model.synthetic.test/v1');await form().getByLabel('模型名',{exact:true}).fill('synthetic-effort');await ready();
 await form().getByLabel('指定思考强度',{exact:true}).check();await form().getByRole('button',{name:'深入 high',exact:true}).click();
 await form().scrollIntoViewIfNeeded();await form().screenshot({path:path.join(output,'ui-settings.png')});
 currentTask=task('synthetic-presentation-task',4);snapshot=processFixture(currentTask.task_id,4);snapshot.search.sources=snapshot.search.sources.slice(0,2);snapshot.search.result_count=2;snapshot.thinking.text='先安排 HTML、CSS 与 JavaScript 基础，再加入组件化实践。\n结合搜索到的参考资料，调整课程的先后顺序与练习难度。';
 await navigate('planning');await panel.getByText('构建课程路线',{exact:true}).waitFor();await panel.getByRole('button',{name:'展开 ⌄',exact:true}).click();await panel.screenshot({path:path.join(output,'ui-process.png')});
} finally {pendingDescribe?.release.resolve();pendingProcess?.release.resolve();await browser.close();}
