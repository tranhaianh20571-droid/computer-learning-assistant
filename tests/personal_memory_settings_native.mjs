// Real authentication and configuration APIs in the runner's isolated schema.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import path from "node:path";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();
page.setDefaultTimeout(15000);
const errors=[];
page.on("pageerror",error=>errors.push(error.message));
const base=process.env.TEST_FRONTEND;
const suffix=Date.now();
const users={alice:`synthetic.alice.${suffix}@example.com`,bob:`synthetic.bob.${suffix}@example.com`};
const password="synthetic-password-12345";
async function login(owner){
  await page.goto(base+"/#/login");
  await page.getByLabel("邮箱",{exact:true}).fill(users[owner]);
  await page.getByLabel("密码",{exact:true}).fill(password);
  await page.getByRole("button",{name:"登录",exact:true}).click();
  await page.waitForURL("**/#/home");
}
async function register(owner){
  await page.goto(base+"/#/register");
  await page.getByLabel("邮箱",{exact:true}).fill(users[owner]);
  await page.getByLabel("密码",{exact:true}).fill(password);
  await page.getByRole("button",{name:"注册账号",exact:true}).click();
  await page.waitForURL("**/#/login");
  await login(owner);
}
async function logout(){
  await page.goto(base+"/#/home");
  await page.getByRole("button",{name:"退出登录",exact:true}).click();
  await page.waitForURL("**/#/login");
}
async function open(){
  await page.goto(base+"/#/capabilities?section=memory");
  await page.getByRole("region",{name:"个人记忆模型配置",exact:true}).waitFor();
}
async function read(){
  return page.evaluate(async()=>({status:(await fetch("/api/memory/models")).status,
    data:await (await fetch("/api/memory/models")).json()}));
}
async function save(owner,protocol){
  await open();
  const fields=page.locator(".memory-provider");
  if(protocol==="jev")await fields.nth(2).getByLabel("Jev",{exact:true}).check();
  for(const [i,kind] of ["content","embedding","rerank"].entries()){
    await fields.nth(i).getByLabel("服务地址",{exact:true}).fill(`https://${kind}.example.test`);
    await fields.nth(i).getByLabel("API Key",{exact:true}).fill(`synthetic-${owner}-${kind}-key`);
    await fields.nth(i).getByLabel("模型",{exact:true}).fill(`synthetic-${owner}-${kind}`);
  }
  await page.getByRole("region",{name:"个人记忆模型配置",exact:true}).getByRole("button",{name:"保存",exact:true}).click();
  await page.getByRole("status").filter({hasText:"已保存 · 仅用于你的资料与记忆"}).waitFor();
  await page.reload();
  await page.getByRole("region",{name:"个人记忆模型配置",exact:true}).waitFor();
  assert.equal(await fields.nth(1).getByLabel("模型",{exact:true}).inputValue(),`synthetic-${owner}-embedding`);
  assert.equal(await fields.nth(1).getByLabel("向量维度",{exact:true}).count(),0);
  assert((await page.locator(".memory-provider input[type=password]").evaluateAll(xs=>xs.map(x=>x.value))).every(x=>x===""));
}
try{
  await register("alice");
  await save("alice","rerank");
  const a=await read();
  assert.equal(a.status,200);
  assert.equal(a.data.rerank_protocol,"rerank");
  assert.equal(await page.evaluate(async()=>(await fetch("/api/memory/engine")).status),403);
  await logout();
  await register("bob");
  await open();
  assert.equal((await read()).data.revision,0);
  await save("bob","jev");
  const b=await read();
  assert.equal(b.data.rerank_protocol,"jev");
  assert.notEqual(a.data.embedding_space,b.data.embedding_space);
  await logout();
  await login("alice");
  await open();
  assert.equal((await read()).data.embedding.model,"synthetic-alice-embedding");
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    await page.locator(".settings-scroll").evaluate(el=>{el.scrollTop=0;});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(process.env.TEST_OUTPUT,`personal-models-${width}.png`),fullPage:true});
    await page.locator(".memory-provider").nth(2).scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(process.env.TEST_OUTPUT,`personal-rerank-${width}.png`),fullPage:true});
  }
  assert.deepEqual(errors,[]);
  console.log("PASS real ordinary-member registration/login/save/reload, A/B private model isolation, Jev/standard choice, masked secrets, administrator boundary, desktop/mobile.");
}finally{await context.close();await browser.close();}
