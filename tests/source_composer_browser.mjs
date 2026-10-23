import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,writeFile} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=process.env.TEST_OUTPUT||".backup/source-imports/browser";
await mkdir(output,{recursive:true});
const errors=[],checks=[];
try{
  for(const width of [1440,768,390,320]){
    const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:"reduce"});
    const page=await context.newPage();
    page.on("pageerror",error=>errors.push(error.message));
    const served=await mockBuiltFrontend(page);
    const calls=[];
    let saved=null;
    await page.route("**/api/**",route=>{
      const request=route.request(),path=new URL(request.url()).pathname;
      calls.push({path,method:request.method()});
      if(path==="/api/auth/me")return route.fulfill({json:{user_id:"source-synthetic",status:"approved",email:"",is_admin:false}});
      if(path==="/api/planning/current")return route.fulfill({json:{planning:null}});
      if(path==="/api/navigation")return route.fulfill({json:{}});
      if(path==="/api/capabilities/model-roles")return route.fulfill({json:{data:["planning","teaching","dialogue","research","research_search","video_understanding"]
        .map(role=>({role,revision:0,config_id:null,configuration:null}))}});
      if(path.startsWith("/api/source-settings/")){
        const provider=path.split("/")[3];
        if(request.method()==="PUT"){
          saved=request.postDataJSON();
          return route.fulfill({json:{provider,revision:1,configuration:{endpoint:saved.endpoint,credential_mask:"****"},options:saved.options}});
        }
        return route.fulfill({json:{provider,revision:0,configuration:null,options:provider==="firecrawl"?
          {enabled:true,keep_images:true,timeout:90,wait_ms:0}:{enabled:true,screenshot:true,link:true,video_understanding:true,interval:6,grid_size:3,max_frames:60,asr_model:"small"}}});
      }
      return route.fulfill({json:{data:[]}});
    });
    await page.goto(served.origin+"/#/home");
    const editor=page.getByLabel("学习目标",{exact:true});
    await editor.waitFor();
    await editor.focus();
    const before=calls.length;
    await editor.evaluate(element=>{
      const clipboard=new DataTransfer();
      clipboard.setData("text/plain","请学习 https://example.org/notes/very-long-synthetic-link?topic=synthetic 然后复习");
      element.dispatchEvent(new ClipboardEvent("paste",{bubbles:true,cancelable:true,clipboardData:clipboard}));
    });
    await page.locator(".source-inline-link").waitFor();
    assert.equal(await page.locator(".source-inline-link").getAttribute("href"),"https://example.org/notes/very-long-synthetic-link?topic=synthetic");
    assert.ok(await editor.textContent().then(text=>text.includes("请学习")&&text.includes("然后复习")));
    await page.locator(".source-inline-link").click();
    await page.getByRole("button",{name:"编辑链接",exact:true}).click();
    const address=page.getByLabel("编辑链接地址",{exact:true});
    await address.fill("https://example.org/edited-synthetic");
    await address.press("Enter");
    assert.equal(await page.locator(".source-inline-link").getAttribute("href"),"https://example.org/edited-synthetic");
    await page.locator(".source-inline-link").click();
    await page.getByRole("button",{name:"移除链接",exact:true}).click();
    assert.equal(await page.locator(".source-inline-link").count(),0);
    await editor.press("Control+z");
    await page.locator(".source-inline-link").waitFor();
    assert.equal(calls.slice(before).filter(call=>call.path.includes("source-import")||call.method==="POST").length,0);
    await editor.evaluate(element=>{
      const clipboard=new DataTransfer();
      clipboard.items.add(new File(["Synthetic Markdown material"],"synthetic.md",{type:"text/markdown"}));
      element.dispatchEvent(new ClipboardEvent("paste",{bubbles:true,cancelable:true,clipboardData:clipboard}));
    });
    await page.getByText("synthetic.md",{exact:true}).waitFor();
    assert.equal(await page.getByLabel("选择本地资料").count(),1);
    assert.equal(calls.slice(before).filter(call=>call.path.includes("source-import")).length,0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
    await page.screenshot({path:`${output}/composer-${width}.png`,fullPage:true});
    await page.goto(served.origin+"/#/capabilities?section=firecrawl");
    await page.getByRole("heading",{name:"Firecrawl",exact:true}).waitFor();
    assert.equal(await page.getByLabel("思考强度",{exact:true}).filter({visible:true}).count(),0);
    await page.locator("#firecrawl-key").fill("synthetic-browser-key");
    await page.locator(".settings-body").getByRole("button",{name:"保存",exact:true}).click();
    await page.getByText("已保存，新任务将使用此配置。",{exact:true}).waitFor();
    assert.equal(saved.api_key,"synthetic-browser-key");
    assert.equal(saved.reasoning_settings,undefined);
    await page.goto(served.origin+"/#/capabilities?section=bilinote");
    await page.getByRole("heading",{name:"BiliNote",exact:true}).waitFor();
    assert.equal(await page.getByRole("checkbox",{name:"视频截图",exact:true}).isChecked(),true);
    assert.equal(await page.getByRole("checkbox",{name:"画面理解",exact:true}).isChecked(),true);
    await page.getByRole("heading",{name:"多模态模型",exact:true}).waitFor();
    const help=page.getByRole("button",{name:"查看多模态模型说明",exact:true});
    await help.click();
    await page.getByRole("tooltip").waitFor();
    assert.equal(await page.getByRole("tooltip").evaluate(element=>element.getBoundingClientRect().right<=innerWidth+1),true);
    await page.keyboard.press("Escape");
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
    await page.screenshot({path:`${output}/bilinote-${width}.png`,fullPage:true});
    checks.push({width,paste:"pass",no_paste_network:"pass",settings:"pass"});
    await context.close();
  }
  assert.deepEqual(errors,[]);
  await writeFile(`${output}/checks.json`,JSON.stringify({checks,errors},null,2));
  console.log(JSON.stringify({checks,errors}));
}finally{await browser.close();}
