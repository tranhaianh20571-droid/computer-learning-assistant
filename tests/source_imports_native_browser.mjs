import assert from "node:assert/strict";
import {createRequire} from "node:module";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const base=process.env.TEST_FRONTEND,backend=process.env.TEST_BACKEND,output=process.env.TEST_OUTPUT;
assert.equal(new URL(backend).hostname,"127.0.0.1");
assert.notEqual(new URL(backend).port,"8000");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[];
try {
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:1000}});
    const page=await context.newPage(),errors=[],external=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.route("**/*",route=>{
      const url=new URL(route.request().url());
      if(["127.0.0.1","localhost"].includes(url.hostname))return route.continue();
      external.push(url.origin);return route.abort();
    });
    try{
      await page.goto(base+"/#/login");
      await page.getByLabel("邮箱",{exact:true}).fill("synthetic-browser-member@example.com");
      await page.getByLabel("密码",{exact:true}).fill("synthetic-password-12345");
      await page.getByRole("button",{name:"登录",exact:true}).click();
      await page.waitForURL("**/#/home");
      const model=await context.request.post(backend+"/api/capabilities/configs",{data:{
        kind:"content_model",protocol:"openai",endpoint:"https://model.example.test/v1",
        model_name:"synthetic-browser-content",credentials:{api_key:"synthetic-browser-key"}}});
      assert.equal(model.status(),201,await model.text());
      await page.getByRole("radio",{name:"即时对话",exact:true}).click();
      const editor=page.getByLabel("你的问题",{exact:true});
      async function pasteFile(target,name){
        await target.focus();
        await target.evaluate((element,name)=>{
          const clipboard=new DataTransfer();
          clipboard.items.add(new File(["# Synthetic database\n\nWhy does a database transaction need atomicity?\nAtomicity applies all changes together.\n"],
            name,{type:"text/markdown"}));
          element.dispatchEvent(new ClipboardEvent("paste",{bubbles:true,cancelable:true,clipboardData:clipboard}));
        },name);
      }
      let imports=0;
      const count=request=>{if(request.method()==="POST"&&new URL(request.url()).pathname==="/api/source-imports")imports++;};
      page.on("request",count);
      await pasteFile(editor,`synthetic-${width}.md`);
      await page.getByText(`synthetic-${width}.md`,{exact:true}).waitFor();
      assert.equal(imports,0);
      await page.getByRole("button",{name:"开始对话",exact:true}).click();
      await page.locator(".turn.ai .body").filter({hasText:"Native SDK synthetic browser answer"}).waitFor({timeout:60000});
      assert.equal(imports,1);
      const conversation=await page.evaluate(()=>sessionStorage.getItem("slice3.convId"));
      const first=await (await context.request.get(backend+"/api/chat/"+conversation)).json();
      assert.equal(first.material_ids.length,1);
      const editor2=page.getByLabel("继续提问",{exact:true});
      await pasteFile(editor2,`followup-${width}.md`);
      await page.getByRole("button",{name:"发送",exact:true}).click();
      await page.waitForFunction(()=>{
        const answers=[...document.querySelectorAll(".turn.ai .body p")];
        return answers.length===2&&answers.every(element=>element.textContent.includes(
          "Native SDK synthetic browser answer: keys identify values and preserve the question context."))&&
          !document.querySelector(".live-cursor");
      },{},{timeout:60000});
      const second=await (await context.request.get(backend+"/api/chat/"+conversation)).json();
      assert.equal(second.material_ids.length,2);
      assert.equal(second.messages.filter(message=>message.role==="assistant"&&message.status==="completed").length,2);
      await page.reload();
      await page.locator(".turn.ai .body").nth(1).waitFor();
      assert.equal(imports,2);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      assert.deepEqual(errors,[]);
      assert.deepEqual(external,[]);
      await page.screenshot({path:`${output}/source-native-${width}.png`,fullPage:true});
      checks.push({width,paste_before_send_no_upload:true,source_only_chat:true,followup_new_material:true,refresh:true});
    }finally{await context.close();}
  }
  console.log(JSON.stringify({checks,real_backend:true}));
}finally{await browser.close();}
