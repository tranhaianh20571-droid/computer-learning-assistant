import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,readFile} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=".backup/feynman-classroom";
await mkdir(output,{recursive:true});
const portrait=await readFile("文档/ui/板书与人物/assets/mentor-neutral.png");
const checks=[];
const profile={name:"沈知行",identity:"课程导师",personality:"沉稳",appearance:"合成人物",
  voice_id:"",media_ids:["image"],base_asset_id:"image"};
const card={card_id:"synthetic-mentor",revision:1,profile};
const text="这段合成教学内容用于检查真实课堂中的板书与费曼问答。请先解释核心机制，再说明必要条件。";
const scenes={
  compare:[{id:"compare",type:"table",title:"进程与线程",columns:["维度","进程","线程"],rows:[["地址空间","通常独立","同进程内共享"],["执行现场","包含线程","各有栈与寄存器"]]}],
  flow:[{id:"flow",type:"graph",diagram:"flowchart",title:"异常处理",span:3,nodes:[{id:"start",label:"执行代码"},{id:"decision",label:"是否抛出异常？",shape:"decision"},{id:"normal",label:"继续后续代码"},{id:"raise",label:"查找匹配处理器"}],edges:[{from_id:"start",to_id:"decision",label:"判断"},{from_id:"decision",to_id:"normal",label:"否"},{from_id:"decision",to_id:"raise",label:"是"}]}],
  sequence:[{id:"sequence",type:"graph",diagram:"sequence",title:"代码如何到达浏览器",span:3,nodes:[{id:"file",label:"代码文件"},{id:"server",label:"本地服务"},{id:"browser",label:"浏览器"}],edges:[{from_id:"browser",to_id:"server",label:"请求页面"},{from_id:"server",to_id:"file",label:"读取文件"},{from_id:"file",to_id:"server",label:"返回代码"},{from_id:"server",to_id:"browser",label:"响应页面"}]}],
  layers:[{id:"layers",type:"layers",title:"同一页面的三种职责",span:3,layers:[{id:"h",label:"HTML",detail:"标题、段落和按钮的内容结构"},{id:"c",label:"CSS",detail:"颜色、间距与排版"},{id:"j",label:"JavaScript",detail:"点击、切换与行为反馈"}]}],
  array:[{id:"array",type:"array",title:"二分查找：保留右半段",span:3,cells:[3,6,11,15,19,23,28,34].map((v,i)=>({id:`a${i}`,value:String(v),index:String(i),state:i<4?"discarded":i===5?"found":"normal",pointer:i===5?"mid":""})),note:"数组有序，才能依据比较结果排除一半范围。"}],
  execution:[{id:"code",type:"code",title:"递归代码",language:"Python",span:3,code:"def fact(n):\n    if n == 1:\n        return 1\n    return n * fact(n - 1)\n\nfact(3)",active_lines:[3]},{id:"stack",type:"stack",title:"每层保留自己的参数",span:3,code_ref:"code",frames:[{id:"f1",label:"fact(1)",variables:["n = 1"],waiting_for:"即将返回 1",active:true},{id:"f2",label:"fact(2)",variables:["n = 2"],waiting_for:"等待 2 × fact(1)"},{id:"f3",label:"fact(3)",variables:["n = 3"],waiting_for:"等待 3 × fact(2)"}]}],
  derivation:[{id:"formula",type:"formula",title:"理想折半到1的轮数",span:3,formula:"n/2^k=1\\quad\\Rightarrow\\quad k=\\log_2 n",format:"latex",plain_text:"n除以2的k次方等于1，因此k等于log2 n"},{id:"trend",type:"chart",chart:"line",title:"规模每翻倍，轮数增加1",labels:["2","4","8","16","32"],values:[[1,2,3,4,5]],is_example:true,span:3}],
};
function board(blocks){return {beats:[{id:"b",text,block_ids:blocks.map(b=>b.id)}],presentation:{version:3,title:"理解机制，再说明条件",blocks}};}
function workspace(blocks){
  return {conversation_id:"synthetic-feynman",kind:"teaching",course_id:"synthetic-course",lesson_id:"synthetic-lesson",
    generation:1,session_state:"active",lesson_title:"合成教学",server_managed:true,tts_authorized:false,audio_mode:"text",
    steps:[{step_no:1,step_id:"s",title:"当前知识点",text,status:"ready",board:board(blocks),text_version_no:1,
      lesson_finished:false,audio_ready:false,character:card}],followups:[],queued_tasks:[],
    last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{paused:true,visible_chars:Array.from(text).length,text_complete:true}},
    feynman:{checkpoints:[],active:null}};
}
function quiz(audio=false){
  return {session_id:"synthetic-session",checkpoint_id:"synthetic-checkpoint",after_step_no:1,revision:1,state:"waiting",
    probe_count:0,max_probes:5,result:"",question_id:"fturn-question",audio_required:audio,
    history:[{id:"fturn-question",role:"assistant",text:"请用自己的话解释：为什么有序数组可以通过比较中间值排除一半范围？",
      source:{kind:"original",page_no:18,end_page:18,chapter:"合成章节：有序查找",answer_basis:"ai"},
      audio_id:audio?"synthetic-feynman-audio":null,subtitles:audio?[{begin_ms:0,end_ms:10000,text:"请用自己的话解释：为什么有序数组可以通过比较中间值排除一半范围？"}]:[],playback:{}}]};
}
try{
  for(const width of [1440,768,390,320]){
    const context=await browser.newContext({viewport:{width,height:900},serviceWorkers:"block"});
    const page=await context.newPage(),errors=[];
    await context.addInitScript(()=>{
      window.__quizMedia=[];
      window.Audio=class{
        paused=true;currentTime=0;duration=10;readyState=4;seeking=false;error=null;preload="";
        constructor(src=""){this.src=src;window.__quizMedia.push(this);}
        play(){this.paused=false;queueMicrotask(()=>this.onplaying?.());return Promise.resolve();}
        pause(){this.paused=true;}
        load(){this.error=null;this.currentTime=0;queueMicrotask(()=>this.onloadedmetadata?.());}
        removeAttribute(name){if(name==="src")this.src="";}
      };
    });
    page.setDefaultTimeout(15000);
    page.on("pageerror",error=>errors.push(error.message));
    let ws=workspace(scenes.compare),lastAnswer=null,started=0,checkpoint=2,completed=0;
    const served=await mockBuiltFrontend(page);
    await page.route("https://esm.sh/**/CascadiaCode-Regular.woff2",async route=>route.fulfill({contentType:"font/woff2",body:await readFile("frontend/node_modules/@excalidraw/excalidraw/dist/prod/fonts/Cascadia/CascadiaCode-Regular.woff2")}));
    const json=(route,value)=>route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(value)});
    await page.route("**/api/**",route=>{
      const p=new URL(route.request().url()).pathname;
      if(p.includes("/assets/image")||p.endsWith("/1/image"))return route.fulfill({contentType:"image/png",body:portrait});
      if(p.endsWith("/character-assets/1"))return json(route,{data:[{asset_id:"image",kind:"image",scene:"neutral",confirmed:true,url:"/api/cards/synthetic-mentor/assets/image"}]});
      if(p==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
      if(p==="/api/cards")return json(route,{data:[card]});
      if(p==="/api/teaching/synthetic-feynman")return json(route,ws);
      if(p.endsWith("/checkpoint")){ws.last_checkpoint={...route.request().postDataJSON(),client_seq:checkpoint++};return json(route,{saved:true});}
      if(p.endsWith("/feynman/start")){
        started++;ws.feynman.active=quiz();
        if(ws.feynman.checkpoints.some(point=>point.scope==="lesson")){
          Object.assign(ws.feynman.active,{scope:"lesson",protocol_version:2,
            learning_state:{current_action:"explain",mode:"check"}});
        }
        return json(route,ws.feynman.active);
      }
      if(p.endsWith("/feynman/playback")){
        const body=route.request().postDataJSON();
        const item=ws.feynman.active?.history.find(item=>item.id===body.turn_id);
        if(item)item.playback={...body};
        return json(route,{saved:true});
      }
      if(p.endsWith("/feynman/answer")){
        lastAnswer=route.request().postDataJSON();
        const q=ws.feynman.active;
        q.revision++;
        q.history.push({id:"user-"+q.revision,role:"user",text:lastAnswer.content});
        q.history.push({id:"feedback-"+q.revision,role:"assistant",text:lastAnswer.help?"先想想“有序”给比较带来什么信息。":"你已说明有序这个关键前提，本次解释较完整。",playback:{}});
        if(!lastAnswer.help){
          q.state="feedback";q.result="understood";
          if(q.scope==="lesson")q.learning_record={initial_explanation:lastAnswer.content,
            dimensions:{accuracy:{status:"demonstrated",evidence:"解释了有序前提。"}},
            transfer_tests:[{question:"换成无序输入呢？",answer:"不能直接排除半边。",feedback:"解释了边界。"}],
            review_items:["复习比较条件。"]};
        }
        return json(route,{task_id:"synthetic-evaluation"});
      }
      if(p.endsWith("/feynman/continue")||p.endsWith("/feynman/skip")){
        const record=ws.feynman.active;
        if(record?.scope==="lesson")ws.feynman.completed=[record];
        ws.feynman.active=null;ws.feynman.checkpoints.forEach(p=>p.resolved=true);return json(route,{state:"closed"});
      }
      if(p.endsWith("/complete")){completed++;return json(route,{completed:true});}
      if(p==="/api/planning/library")return json(route,{routes:[],courses:[],drafts:[]});
      return json(route,{data:[]});
    });
    await page.goto(served.origin+"/#/lecture/synthetic-feynman");
    for(const [name,blocks] of Object.entries(scenes)){
      ws.steps[0].board=board(blocks);
      const id=blocks.at(-1).id;
      await page.waitForFunction(id=>document.querySelector(".teaching-board-canvas")?.dataset.visibleBlocks.includes(id),id);
      await page.locator(".classroom-character img").waitFor();
      await page.evaluate(()=>document.fonts.ready);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),"no page overflow");
      await page.waitForFunction(()=>[...document.querySelectorAll("canvas")].some(canvas=>{
        const ctx=canvas.getContext("2d");if(!ctx||!canvas.width)return false;
        const data=ctx.getImageData(0,0,canvas.width,canvas.height).data,colors=new Set();
        for(let i=0;i<data.length;i+=160)colors.add(`${data[i]},${data[i+1]},${data[i+2]}`);
        return colors.size>10;
      })).catch(async error=>{await page.screenshot({path:`${output}/failed-${name}-${width}.png`});throw error;});
      await page.screenshot({path:`${output}/${name}-${width}.png`});
      checks.push(`${name}_${width}`);
    }
    ws.feynman.active=quiz();
    await page.getByLabel("回答本题",{exact:true}).waitFor();
    await page.locator(".question-source").getByText("资料原题",{exact:true}).waitFor();
    assert((await page.locator(".question-source").innerText()).includes("第 18 页"));
    assert((await page.locator(".question-source").innerText()).includes("评分依据由 AI 拟定"));
    checks.push(`source_provenance_${width}`);
    await page.getByRole("button",{name:"先问一下",exact:true}).click();
    await page.getByLabel("向导师求助",{exact:true}).fill("我不理解有序为什么重要。");
    await page.getByRole("button",{name:"发送求助",exact:true}).click();
    await page.getByLabel("回答本题",{exact:true}).waitFor();
    assert(lastAnswer.help);
    await page.getByLabel("回答本题",{exact:true}).fill("因为有序，中间值左侧都不大于它，目标更大时就能排除左侧。");
    await page.getByRole("button",{name:"提交回答",exact:true}).click();
    await page.getByRole("button",{name:"继续讲解",exact:true}).waitFor();
    assert(!lastAnswer.help);
    await page.screenshot({path:`${output}/answered-${width}.png`});
    await page.reload();
    await page.getByRole("button",{name:"继续讲解",exact:true}).waitFor();
    assert.equal(await page.locator(".feynman-user").count(),2);
    ws.feynman.active.probe_count=5;ws.feynman.active.result="review";
    await page.getByText("追问 5 / 5",{exact:true}).waitFor();
    await page.screenshot({path:`${output}/fifth-feedback-${width}.png`});
    checks.push(`input_help_answer_refresh_limit_${width}`);
    ws.feynman.active=null;
    ws.feynman.checkpoints=[{checkpoint_id:"synthetic-checkpoint",after_step_no:1,resolved:false}];
    ws.last_checkpoint={client_seq:checkpoint++,step_no:1,position_ms:0,presentation:{
      paused:false,text_complete:true,visible_chars:Array.from(text).length}};
    await page.reload();
    await page.getByLabel("回答本题",{exact:true}).waitFor();
    assert.equal(started,1,"a completed knowledge boundary starts exactly one dialogue");
    checks.push(`automatic_knowledge_boundary_${width}`);
    ws.feynman.active=quiz(true);
    ws.feynman.active.history[0].id="fturn-audio";
    ws.feynman.active.question_id="fturn-audio";
    await page.reload();
    await page.getByRole("button",{name:"播放问答语音",exact:true}).waitFor();
    await page.waitForFunction(()=>window.__quizMedia.some(a=>a.src.includes("synthetic-feynman-audio")));
    await page.getByRole("button",{name:"播放问答语音",exact:true}).click();
    await page.evaluate(()=>{window.__quizMedia.find(a=>a.src.includes("synthetic-feynman-audio")).currentTime=4;});
    await page.waitForFunction(()=>document.querySelector(".feynman-teacher p").textContent.length>3);
    await page.getByRole("button",{name:"停止讲解",exact:true}).click();
    const stoppedQuestion=await page.locator(".feynman-teacher p").innerText();
    await page.evaluate(()=>{
      const audio=window.__quizMedia.find(a=>a.src.includes("synthetic-feynman-audio"));
      audio.currentTime=9;audio.onplaying?.();audio.onended?.();
    });
    await page.waitForTimeout(3100);
    assert.equal(await page.locator(".feynman-teacher p").innerText(),stoppedQuestion,
      "global stop rejects late playing and ended callbacks");
    assert.equal(await page.getByText("已播放完毕",{exact:true}).count(),0);
    await page.evaluate(()=>{window.__quizMedia.find(a=>a.src.includes("synthetic-feynman-audio")).currentTime=4;});
    await page.getByRole("button",{name:"播放问答语音",exact:true}).click();
    checks.push(`global_question_stop_rejects_late_events_${width}`);
    await page.evaluate(()=>{const a=window.__quizMedia.find(a=>a.src.includes("synthetic-feynman-audio"));a.pause();a.error={code:2};a.onerror();});
    const frozen=await page.locator(".feynman-teacher p").innerText();
    await page.waitForTimeout(250);
    assert.equal(await page.locator(".feynman-teacher p").innerText(),frozen);
    await page.getByRole("button",{name:"播放问答语音",exact:true}).click();
    await page.evaluate(()=>{window.__quizMedia.find(a=>a.src.includes("synthetic-feynman-audio")).currentTime=8;});
    await page.waitForFunction(length=>document.querySelector(".feynman-teacher p").textContent.length>length,frozen.length);
    await page.evaluate(()=>{const a=window.__quizMedia.find(a=>a.src.includes("synthetic-feynman-audio"));a.currentTime=10;a.pause();a.onended();});
    await page.getByText("已播放完毕",{exact:true}).waitFor();
    checks.push(`question_audio_freeze_retry_end_${width}`);
    ws=workspace(scenes.compare);
    ws.steps[0].lesson_finished=true;
    ws.feynman.checkpoints=[{checkpoint_id:"lesson-synthetic",scope:"lesson",after_step_no:1,resolved:false}];
    await page.reload();
    await page.getByRole("button",{name:"开始本讲费曼对话",exact:true}).click();
    await page.getByLabel("本讲费曼对话测验",{exact:true}).waitFor();
    assert.equal(await page.getByRole("button",{name:"跳过本次问答",exact:true}).count(),0);
    assert.equal(await page.getByRole("button",{name:"确认本讲已学完",exact:true}).count(),0);
    await page.reload();
    await page.getByLabel("本讲费曼对话测验",{exact:true}).waitFor();
    await page.getByLabel("回答本题",{exact:true}).fill("有序才能利用比较结果排除另一半，换成无序就不成立。");
    await page.getByRole("button",{name:"提交回答",exact:true}).click();
    await page.getByRole("button",{name:"完成本讲测验",exact:true}).click();
    await page.getByText("本讲学习记录",{exact:true}).click();
    await page.getByRole("heading",{name:"最初的解释",exact:true}).waitFor();
    await page.getByRole("heading",{name:"新情境检验",exact:true}).waitFor();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/lesson-record-${width}.png`});
    await page.getByRole("button",{name:"确认本讲已学完",exact:true}).click();
    await page.waitForURL("**/#/knowledge/synthetic-feynman");
    assert.equal(completed,1);
    checks.push(`lesson_required_resume_record_complete_${width}`);
    assert.deepEqual(errors,[]);
    await context.close();
  }
}finally{await browser.close();}
console.log(JSON.stringify({checks,synthetic:true,realProviderTested:false}));
