// Explicit local live smoke: normal registration/login and published mentor views.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,writeFile} from "node:fs/promises";
import crypto from "node:crypto";

assert(process.argv.includes("--live"),"explicit --live is required");
const origin=process.env.TEST_FRONTEND||"http://127.0.0.1:5173";
assert.equal(new URL(origin).hostname,"127.0.0.1");
const cardId=process.env.TEST_CHARACTER_ID;
assert(cardId);
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:"block"});
const output=".backup/persona-evidence";
await mkdir(output,{recursive:true});
const page=await context.newPage(),errors=[],checks=[];
let userId=null;
page.on("pageerror",error=>errors.push(error.message));
await page.route("**/*",route=>["127.0.0.1","localhost"].includes(new URL(route.request().url()).hostname)?
  route.continue():route.abort());
try{
  const email=`synthetic-persona-${crypto.randomUUID()}@example.com`;
  const password="Synthetic-"+crypto.randomUUID();
  const registered=await context.request.post(origin+"/api/auth/register",
    {data:{email,password,display_name:"Synthetic mentor validation"}});
  assert.equal(registered.status(),201);
  userId=(await registered.json()).user_id;
  await page.goto(origin+"/#/login");
  await page.getByLabel("邮箱",{exact:true}).fill(email);
  await page.getByLabel("密码",{exact:true}).fill(password);
  await page.getByRole("button",{name:"登录",exact:true}).click();
  await page.waitForURL("**/#/home");
  const preference=await context.request.get(origin+"/api/mentor-preference");
  assert(preference.ok());
  const selected=await preference.json();
  assert.equal(selected.card_id,cardId);
  assert.equal(selected.using_system_default,true);
  checks.push("normal-login-and-shared-default");
  const response=await context.request.get(origin+"/api/cards/"+cardId);
  assert(response.ok());
  const card=await response.json();
  assert.equal(card.system_default,true);
  assert.equal(card.revision,card.published_revision);
  assert.equal(card.persona.quality.passed,true);
  assert.equal(card.persona.quality.default_eligible,true);
  assert.equal(card.persona.references.length,6);
  assert.equal(card.profile.media_ids.length,8);
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    await page.goto(origin+"/#/card-market");
    await page.getByRole("button",{name:"系统默认导师",exact:true}).waitFor();
    await page.waitForFunction(()=>[...document.querySelectorAll(".character-cover img")]
      .every(image=>image.complete&&image.naturalWidth>0));
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/published-market-${width}.png`,fullPage:true});
    await page.goto(origin+"/#/card/"+cardId);
    await page.getByLabel("完整人物 Skill",{exact:true}).waitFor();
    assert.equal(await page.getByLabel("完整人物 Skill",{exact:true}).inputValue(),card.persona.skill);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/published-card-${width}.png`,fullPage:true});
    checks.push(`live-published-full-persona-${width}`);
  }
  assert.deepEqual(errors,[]);
  await writeFile(output+"/live-card-browser.json",JSON.stringify({
    checks,errors,user_id:userId,card_id:cardId,revision:card.revision,realBackend:true},null,2));
  console.log(JSON.stringify({checks,errors,user_id:userId,revision:card.revision}));
}finally{
  if(userId)await writeFile(output+"/smoke-user.json",JSON.stringify({user_id:userId}));
  await context.close();
  await browser.close();
}
