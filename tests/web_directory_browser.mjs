import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,writeFile} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=process.env.TEST_OUTPUT||".backup/web-directory/browser";
await mkdir(output,{recursive:true});
const checks=[],errors=[];
try{
  for(const width of [1440,390,320]){
    const context=await browser.newContext({viewport:{width,height:850},reducedMotion:"reduce"});
    const page=await context.newPage();
    page.on("pageerror",error=>errors.push(error.message));
    const served=await mockBuiltFrontend(page);
    let scenario="directory",created=null,discovered=0;
    const chapters=[
      {url:"https://example.org/intro",title:"1. 引言",depth:0},
      {url:"https://example.org/basics",title:"2. 预备知识",depth:0},
      {url:"https://example.org/basics/data",title:"2.1 数据操作与一段很长的章节标题用于检查手机屏幕自动换行",depth:1},
    ];
    function batch(){
      return {batch_id:"synthetic-batch",status:"draft",manifest_hash:"a".repeat(64),
        items:(created?.items||[]).map((item,index)=>({...item,item_id:"synthetic-"+index,kind:"web",
          status:"uploaded",chapter_count:item.chapter_urls?.length||0})),
        destinations:[{config_id:"synthetic-reader",endpoint:"https://example.org",config_version:1,purpose:"reader"}]};
    }
    await page.route("**/api/**",route=>{
      const req=route.request(),path=new URL(req.url()).pathname;
      if(path==="/api/auth/me")return route.fulfill({json:{user_id:"synthetic-directory",status:"approved",is_admin:false}});
      if(path==="/api/planning/current")return route.fulfill({json:{planning:null}});
      if(path==="/api/navigation")return route.fulfill({json:{}});
      if(path==="/api/source-directory"){
        discovered++;
        if(scenario==="failure")return route.fulfill({status:422,json:{error_code:"invalid_field",message:"目录读取失败，请重试。"}});
        return route.fulfill({json:{url:"https://example.org/",chapters:scenario==="single"?[]:chapters,truncated:false}});
      }
      if(path==="/api/source-imports"&&req.method()==="POST"){
        created=req.postDataJSON();
        return route.fulfill({status:201,json:batch()});
      }
      if(path.startsWith("/api/source-imports/"))return route.fulfill({json:batch()});
      return route.fulfill({json:{matches:[],data:[]}});
    });
    async function enter(){
      await page.goto(served.origin+"/#/home");
      await page.getByRole("radio",{name:"即时对话",exact:true}).click();
      const editor=page.getByLabel("你的问题",{exact:true});
      await editor.focus();
      await editor.evaluate(element=>{
        const data=new DataTransfer();data.setData("text/plain","https://example.org/");
        element.dispatchEvent(new ClipboardEvent("paste",{bubbles:true,cancelable:true,clipboardData:data}));
      });
      await page.locator(".source-inline-link").waitFor();
    }
    async function reset(){
      await page.evaluate(()=>sessionStorage.clear());
      await page.reload();
      created=null;
      await enter();
    }
    await enter();
    assert.equal(discovered,0);
    await page.getByRole("button",{name:"开始对话",exact:true}).click();
    const dialog=page.getByRole("dialog",{name:"选择章节",exact:true});
    await dialog.waitFor();
    assert.equal(await dialog.getByRole("checkbox",{name:"全选章节",exact:true}).evaluate(
      element=>element===document.activeElement),true);
    await page.keyboard.press("Shift+Tab");
    assert.equal(await dialog.getByRole("button",{name:"仅导入当前页",exact:true}).evaluate(
      element=>element===document.activeElement),true);
    await page.keyboard.press("Tab");
    assert.equal(await dialog.getByRole("checkbox",{name:"全选章节",exact:true}).evaluate(
      element=>element===document.activeElement),true);
    const submit=dialog.getByRole("button",{name:"导入所选章节",exact:true});
    assert.equal(await submit.isDisabled(),true);
    const all=dialog.getByRole("checkbox",{name:"全选章节",exact:true});
    await all.check();
    await dialog.getByText("已选 3 / 3 页",{exact:true}).waitFor();
    await all.uncheck();
    await dialog.getByRole("checkbox",{name:"2. 预备知识",exact:true}).check();
    await dialog.getByText("已选 2 / 3 页",{exact:true}).waitFor();
    await page.screenshot({path:`${output}/chapters-${width}.png`,fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
    await submit.click();
    await page.getByRole("dialog",{name:"确认处理资料",exact:true}).waitFor();
    assert.deepEqual(created.items[0].chapter_urls,chapters.slice(1).map(c=>c.url));
    await page.getByText("所选网页共 2 页。",{exact:true}).waitFor();
    await page.getByRole("dialog",{name:"确认处理资料",exact:true}).getByRole("button",{name:"取消",exact:true}).click();
    await reset();
    await page.getByRole("button",{name:"开始对话",exact:true}).click();
    await page.getByRole("dialog",{name:"选择章节",exact:true}).getByRole("button",{name:"仅导入当前页",exact:true}).click();
    await page.getByRole("dialog",{name:"确认处理资料",exact:true}).waitFor();
    assert.equal(created.items[0].chapter_urls,undefined);
    // Fresh composition: cancelling selection sends nothing to the import API.
    await reset();
    await page.getByRole("button",{name:"开始对话",exact:true}).click();
    await page.getByRole("dialog",{name:"选择章节",exact:true}).waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("button",{name:"开始对话",exact:true}).waitFor();
    assert.equal(created,null);
    // No directory continues directly to the normal single-page consent.
    scenario="single";
    await reset();
    await page.getByRole("button",{name:"开始对话",exact:true}).click();
    await page.getByRole("dialog",{name:"确认处理资料",exact:true}).waitFor();
    assert.equal(created.items[0].chapter_urls,undefined);
    assert.equal(await page.getByRole("dialog",{name:"选择章节",exact:true}).count(),0);
    // Discovery errors must not be silently treated as no directory.
    scenario="failure";
    await reset();
    await page.getByRole("button",{name:"开始对话",exact:true}).click();
    await page.getByText("目录读取失败，请重试。",{exact:true}).waitFor();
    assert.equal(created,null);
    checks.push({width,selection:"pass",all:"pass",cancel:"pass",single_page:"pass",failure:"pass"});
    await context.close();
  }
  assert.deepEqual(errors,[]);
  await writeFile(`${output}/checks.json`,JSON.stringify({checks,errors},null,2));
  console.log(JSON.stringify({checks,errors}));
}finally{await browser.close();}
