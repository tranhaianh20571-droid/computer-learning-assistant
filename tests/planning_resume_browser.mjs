import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdirSync,writeFileSync} from 'node:fs';
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+'/playwright');
const base=process.env.TEST_FRONTEND,backend=process.env.TEST_BACKEND,output=process.env.TEST_OUTPUT;
assert(['127.0.0.1','localhost'].includes(new URL(backend).hostname));assert.notEqual(new URL(backend).port,'8000');
assert.notEqual(new URL(base).port,'5173');mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1600,height:1000}}),page=await context.newPage(),errors=[],checks=[];
page.on('pageerror',e=>errors.push(e.message));
async function api(path,method='GET',body){return page.evaluate(async({url,method,body})=>{
 const r=await fetch(url,{method,credentials:'include',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
 if(!r.ok)throw Error('HTTP '+r.status);return r.json();
},{url:backend+path,method,body});}
async function nav(route){await page.goto(base+'/#/'+route);}
async function start(title){
 await nav('home');await page.locator('#goal').waitFor();await page.locator('#goal').fill(title);
 await page.getByRole('button',{name:'生成规划草稿',exact:true}).click();
 await page.locator('.q-card').nth(3).waitFor({timeout:20000});
 return (await api('/api/planning/current')).planning.task_id;
}
async function fill(){for(let i=0;i<4;i++)await page.locator('.q-card').nth(i).locator('.opt-card').first().click();}
async function abandon(){await page.getByRole('button',{name:'放弃规划',exact:true}).first().click();await page.locator('#goal').waitFor();}
try{
 const task=await start('合成延迟恢复验证');
 await page.locator('.q-card').first().locator('.opt-card').first().click();
 await page.locator('.q-card').nth(1).getByRole('button',{name:'都不太符合？自己补充'}).click();
 await page.getByLabel('补充 已有相关经验的实际使用情况更接近哪一种？',{exact:true}).fill('合成未提交回答');
 await page.getByText('回答草稿已保存',{exact:true}).waitFor();
 await nav('home');await nav('planning');await page.locator('.q-card').nth(3).waitFor();
 assert.equal(await page.locator('.q-card').first().locator('.opt-card').first().getAttribute('aria-pressed'),'true');
 assert.equal(await page.getByLabel('补充 已有相关经验的实际使用情况更接近哪一种？',{exact:true}).inputValue(),'合成未提交回答');
 await page.reload();await page.locator('.q-card').nth(3).waitFor();
 assert.equal((await api('/api/planning/current')).planning.task_id,task);checks.push('leave_and_reload_restore_same_task_and_unsubmitted_answers');
 await fill();await page.getByRole('button',{name:'提交并继续',exact:true}).click();
 await page.getByText('生成在后台继续，可以暂时离开，回来后会自动恢复进度。').waitFor();
 await nav('home');await nav('planning');
 await page.getByRole('button',{name:'没问题，确认草稿',exact:true}).waitFor({timeout:25000});
 assert.equal((await api('/api/planning/current')).planning.task_id,task);checks.push('background_generation_survives_navigation_without_new_task');
 await page.reload();await page.getByRole('button',{name:'没问题，确认草稿',exact:true}).waitFor();checks.push('draft_restored_after_reload');
 const second=await context.newPage();await second.goto(base+'/#/planning');await second.getByRole('button',{name:'没问题，确认草稿',exact:true}).waitFor();
 await abandon();await second.getByRole('button',{name:'生成路线候选',exact:true}).waitFor();
 await second.waitForFunction(()=>!document.querySelector('.mm-board'));
 assert.equal((await api('/api/planning/current')).planning,null);await second.reload();
 assert.equal(await second.getByRole('button',{name:'没问题，确认草稿',exact:true}).count(),0);await second.close();
 checks.push('abandon_syncs_other_tab_and_does_not_reappear');
 const failure=await start('合成失败自动续试验证');await fill();await page.getByRole('button',{name:'提交并继续',exact:true}).click();
 await page.getByRole('alertdialog').waitFor();
 assert.equal((await api('/api/planning/tasks/'+failure)).error_code,'MODEL_PROVIDER_ERROR');
 await page.getByRole('alertdialog').getByRole('button',{name:'暂时离开',exact:true}).click();await nav('planning');
 await page.getByRole('button',{name:'没问题，确认草稿',exact:true}).waitFor({timeout:25000});
 const recovered=await api('/api/planning/tasks/'+failure);assert.equal(recovered.auto_retry_count,1);assert.equal(recovered.intake.answers.length,4);
 checks.push('failure_reentry_resumes_once_with_saved_answers');await abandon();
 const cancelled=await start('合成延迟放弃验证');await fill();await page.getByRole('button',{name:'提交并继续',exact:true}).click();
 await page.getByText('生成在后台继续，可以暂时离开，回来后会自动恢复进度。').waitFor();await abandon();
 await page.waitForTimeout(5000);await nav('planning');
 assert.equal((await api('/api/planning/tasks/'+cancelled)).task_status,'cancelled');
 assert.equal((await api('/api/planning/current')).planning,null);checks.push('abandon_running_rejects_late_output');
 await page.setViewportSize({width:390,height:844});await page.reload();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));checks.push('mobile_no_horizontal_overflow');
 assert.deepEqual(errors,[]);await page.screenshot({path:output+'/planning-resume.png',fullPage:true});
 writeFileSync(output+'/results.json',JSON.stringify({checks,pageErrors:errors,synthetic:true},null,2));console.log(JSON.stringify({checks,pageErrors:errors}));
}finally{await browser.close();}
