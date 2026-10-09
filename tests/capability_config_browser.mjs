import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+'/playwright-core');
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[],checks=[],requests=[];
let deleted=false,failDelete=true,patch=null,createdPayload=null,creates=0;
let config={config_id:'synthetic-model',kind:'content_model',protocol:'openai',endpoint:'https://model.example.test/v1',model_name:'synthetic-model',credential_mask:'synthetic-****',capability_status:{text:{state:'unknown',detail:'connection_error'}},owner_scope:'user',config_version:1,is_active:true};
page.on('pageerror',e=>errors.push(e.message));
page.on('dialog',()=>{throw new Error('Native dialog must not be used');});
await page.route('**/api/**',async route=>{
 const req=route.request(),path=new URL(req.url()).pathname,method=req.method();requests.push({path,method});
 let status=200,body={data:[]};
 if(path.endsWith('/auth/me')||path.endsWith('/auth/guest')) body={user_id:'synthetic-user',email:'synthetic@example.test',is_admin:false,status:'approved'};
 else if(path.endsWith('/capabilities/configs')&&method==='GET') body={data:deleted?[]:[config]};
 else if(path.endsWith('/capabilities/configs')&&method==='POST'){creates++;createdPayload=req.postDataJSON();body={...config,...createdPayload};status=201;}
 else if(path.endsWith('/synthetic-model')&&method==='PATCH'){patch=req.postDataJSON();config={...config,...patch};body=config;}
 else if(path.endsWith('/synthetic-model')&&method==='DELETE'){
  if(failDelete){status=503;body={detail:{code:'synthetic_failure',message:'合成删除失败，请重试'}};failDelete=false;}
  else{deleted=true;body={ok:true};}
 }
 else if(path.endsWith('/synthetic-model/test'))body={capability_status:{text:{state:'unavailable',detail:'credential_invalid'}}};
 await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
});
try{
 await page.goto((process.env.TEST_FRONTEND||'http://127.0.0.1:5173')+'/#/capabilities');
 await page.getByRole('button',{name:'编辑',exact:true}).waitFor();
 await page.getByText('连接失败，请检查网络和代理',{exact:true}).waitFor();checks.push('visible_probe_failure_reason');
 const form=page.locator('.card').filter({has:page.getByRole('heading',{name:'个人内容模型',exact:true})});
 assert(await form.getByRole('button',{name:'保存模型配置',exact:true}).isDisabled());
 await form.getByLabel('模型名',{exact:true}).fill('  synthetic-new  ');
 await form.getByLabel('API Key',{exact:true}).fill('synthetic-key');
 await form.getByRole('button',{name:'保存模型配置',exact:true}).click();
 await page.waitForFunction(()=>[...document.querySelectorAll('.card')].find(x=>x.querySelector('h2')?.textContent==='个人内容模型')?.querySelector('input[type=password]')?.value==='');
 assert.equal(creates,1);assert.equal(createdPayload.model_name,'synthetic-new');checks.push('save_validation_trims_values_and_clears_key');
 await page.getByRole('button',{name:'编辑',exact:true}).click();
 let dialog=page.getByRole('dialog');await dialog.waitFor();
 await dialog.getByLabel('模型名',{exact:true}).fill('  edited-model  ');
 await dialog.getByRole('button',{name:'保存修改',exact:true}).click();await dialog.waitFor({state:'hidden'});
 assert.equal(patch.model_name,'edited-model');assert(!Object.hasOwn(patch,'credentials'));checks.push('edit_preserves_key_and_trims_values');
 await page.getByRole('button',{name:'测试能力',exact:true}).click();
 await page.getByText('凭据被服务拒绝，请更新 API Key',{exact:true}).waitFor();checks.push('test_result_failure_visible');
 await page.getByRole('button',{name:'删除',exact:true}).click();
 dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'取消',exact:true}).click();
 assert.equal(requests.filter(r=>r.method==='DELETE').length,0);checks.push('delete_cancel_makes_no_request');
 await page.getByRole('button',{name:'删除',exact:true}).click();
 await page.getByRole('dialog').getByRole('button',{name:'确认删除',exact:true}).click();
 await page.getByText('合成删除失败，请重试',{exact:true}).waitFor();
  await page.getByRole('dialog').getByRole('alert').waitFor();
 assert(!deleted);checks.push('delete_error_keeps_config');
 await page.getByRole('dialog').getByRole('button',{name:'确认删除',exact:true}).click();
 await page.getByRole('button',{name:'编辑',exact:true}).waitFor({state:'hidden'});assert(deleted);checks.push('delete_success_removes_config');
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));checks.push('mobile_no_horizontal_overflow');
 assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,synthetic:true,pageErrors:errors}));
}catch(e){console.log(JSON.stringify({checks,pageErrors:errors}));throw e;}
finally{await browser.close();}
