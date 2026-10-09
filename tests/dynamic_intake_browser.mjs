import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdirSync,writeFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+'/playwright');
const backend=process.env.TEST_BACKEND,base='http://127.0.0.1:5173',output=process.env.TEST_OUTPUT;
assert.equal(new URL(backend).hostname,'127.0.0.1');assert.notEqual(new URL(backend).port,'8000');mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1600,height:1000}}),page=await context.newPage(),errors=[],checks=[];
page.on('pageerror',error=>errors.push(error.message));
await context.addInitScript(({backend})=>{
 const original=window.fetch.bind(window);window.fetch=(input,init)=>original(typeof input==='string'&&input.startsWith('/api/')?backend+input:input,init);
},{backend});
async function start(request){
 await page.goto(base+'/#/capabilities');await page.getByRole('heading',{name:'能力配置',exact:true}).waitFor();
 const result=await page.evaluate(async request=>{
  const route=await (await fetch('/api/planning/routes',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'合成Java学习',input:request})})).json();
  const task=await (await fetch('/api/planning/tasks',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_kind:'route_generate',planning_scope:'auto',target_route_id:route.route_id,idempotency_key:crypto.randomUUID()})})).json();
  sessionStorage.setItem('slice3.pendingTask',task.task_id);sessionStorage.setItem('slice3.pendingRoute',route.route_id);return task.task_id;
 },request);
 await page.goto(base+'/#/planning');await page.locator('.q-card').nth(3).waitFor({timeout:30000});return result;
}
try{
 const task=await start('希望详细学习Java，有C++使用经验，但不熟悉所有概念。');
 assert.equal(await page.locator('.q-card').count(),4);assert.equal(await page.getByText(/至多.*轮/).count(),0);
 assert.equal(await page.getByText('单门课程',{exact:true}).count(),0);assert.equal(await page.getByText('多课程路线',{exact:true}).count(),0);
 assert.equal(await page.getByRole('button',{name:'都不太符合？自己补充',exact:true}).count(),4);checks.push('four_ai_questions_without_scope_selection_or_round_limit');
 for(let index=0;index<4;index++)await page.locator('.q-card').nth(index).locator('.opt-card').first().click();
 await page.locator('.q-card').nth(1).getByRole('button',{name:'都不太符合？自己补充',exact:true}).click();
 await page.getByLabel('补充 已有相关经验的实际使用情况更接近哪一种？',{exact:true}).fill('做过小程序；复杂问题仍需要查资料。');
 await page.getByRole('button',{name:'提交并继续',exact:true}).click();
 await page.waitForFunction(()=>document.querySelectorAll('.q-card').length===2,null,{timeout:30000});
 assert(await page.getByText(/已展示 6 个问题/).count());checks.push('followups_stay_within_six_and_free_text_is_retained');
 await page.reload();await page.locator('.q-card').nth(1).waitFor();
 const snapshot=await page.evaluate(async task=>(await (await fetch('/api/planning/tasks/'+task,{credentials:'include'})).json()).intake,task);
 assert.equal(snapshot.history.length,6);assert(snapshot.answers.some(answer=>answer.free_text.includes('复杂问题')));checks.push('refresh_restores_history_and_answer_labels');
 for(let index=0;index<2;index++)await page.locator('.q-card').nth(index).getByRole('button',{name:'跳过这题',exact:true}).click();
 await page.getByRole('button',{name:'提交并继续',exact:true}).click();
 await page.getByRole('button',{name:'没问题，确认草稿',exact:true}).waitFor({timeout:30000});
 assert.equal(await page.locator('.q-card').count(),0);checks.push('skipped_followups_finish_without_extra_question');
 const state=await page.evaluate(async task=>(await (await fetch('/api/planning/tasks/'+task,{credentials:'include'})).json()),task);
 assert(state.intake.done&&state.intake.shown_count===6&&state.intake.learner_profile.detail_status==='confirmed');
 checks.push('profile_contains_depth_and_prior_experience');
 const single=await start('单门验证：详细学习Java语法，有一些C++经历。');
 for(let index=0;index<4;index++)await page.locator('.q-card').nth(index).getByRole('button',{name:'跳过这题',exact:true}).click();
 assert(await page.getByRole('button',{name:'提交并继续',exact:true}).isEnabled());
 await page.getByRole('button',{name:'提交并继续',exact:true}).click();
 await page.waitForFunction(()=>document.querySelectorAll('.q-card').length===2,null,{timeout:30000});
 await page.getByRole('button',{name:'提交并继续',exact:true}).click();
 await page.getByRole('button',{name:'没问题，确认草稿',exact:true}).waitFor({timeout:30000});
 const course=await page.evaluate(async task=>(await (await fetch('/api/planning/tasks/'+task,{credentials:'include'})).json()),single);
 assert.equal(course.target_kind,'course');assert.equal(course.intake.learner_profile.detail_status,'assumed');
 checks.push('all_skipped_is_allowed_and_ai_selects_course_internally');
 await page.getByRole('button',{name:'没问题，确认草稿',exact:true}).click();
 await page.getByRole('button',{name:'查看课程总览 →',exact:true}).waitFor();await page.getByRole('button',{name:'查看课程总览 →',exact:true}).click();
 await page.getByRole('button',{name:'开始学习',exact:true}).first().waitFor();checks.push('course_confirmation_and_navigation_use_ai_course_target');
 await page.setViewportSize({width:390,height:844});await page.reload();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 checks.push('mobile_has_no_horizontal_overflow');assert.deepEqual(errors,[]);
 writeFileSync(output+'/dynamic-intake-browser.json',JSON.stringify({checks,pageErrors:errors,synthetic:true},null,2));console.log(JSON.stringify({checks,pageErrors:errors}));
}finally{await browser.close();}
