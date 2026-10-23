// Production build with synthetic APIs; no live account or provider changes.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,readFile} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=process.env.TEST_OUTPUT||".backup/course-review";
await mkdir(output,{recursive:true});
const cover=await readFile("文档/ui/复习/assets/course-agents.png");
const errors=[];
try {
  for(const width of [1440,768,390,320]){
    const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:"reduce"});
    page.setDefaultTimeout(12000);
    page.on("pageerror",e=>errors.push(e.message));
    const served=await mockBuiltFrontend(page);
    let fail=false,answerCalls=0,draft={},session=null;
    const counts={overdue:2,today:3,week:5,later:2};
    const course={id:"course-synthetic",kind:"course",title:"合成课程：知识点与反馈验证",unit_count:4,counts};
    const child={...course,id:"child-synthetic",title:"合成子课程：多智能体协作",cover_owner_kind:"route",cover_owner_id:"route-synthetic"};
    const overview={total:24,counts:{overdue:4,today:6,week:10,later:4},due_course_ids:[course.id,child.id],resume_session_id:null,
      week:Array.from({length:7},(_,i)=>({date:`2026-10-${23+i}`,count:i%3+1})),
      data:[course,{id:"route-synthetic",kind:"route",title:"合成合集：多智能体工程实践",course_count:1,counts,courses:[child]}]};
    await page.route("**/api/**",async route=>{
      const req=route.request(),path=new URL(req.url()).pathname,method=req.method(),body=req.postData()?req.postDataJSON():{};
      const json=(value,status=200)=>route.fulfill({json:value,status});
      if(path==="/api/auth/me")return json({user_id:"review-owner",name:"合成用户",status:"approved",is_admin:false});
      if(path==="/api/navigation")return json({});
      if(path==="/api/market/profile")return json({bound:false});
      if(path==="/api/capabilities/configs"||path==="/api/capabilities/model-roles"||path==="/api/connectors")return json({data:[]});
      if(path.includes("/cover")&&!path.endsWith("/image"))return json({status:"succeeded",image_url:"/api/review-test/image",cover_eligible:true});
      if(path==="/api/review-test/image")return route.fulfill({body:cover,contentType:"image/png"});
      if(path==="/api/review/overview")return fail?json({error_code:"INTERNAL_ERROR"},500):json(overview);
      if(path.endsWith("/points"))return json({data:[{record_id:"point",title:"合成知识点",due_at:"2026-10-22T00:00:00Z",kind:"today"}]});
      if(path==="/api/review/sessions"&&method==="POST"){
        session={session_id:"review-session",course_id:body.course_id,title:course.title,revision:1,position:0,total:1,status:"ready",
          question:{record_id:"point",prompt:"为什么提出改进方案后还要验证实际结果？",source:{kind:"generated",reason:"合成测试题。"}},
          answer:"",draft:{},assisted:false,feedback:null};
        return json(session,202);
      }
      if(path.endsWith("/draft")&&method==="PUT"){draft=body;session.draft={answer:body.answer,rating:body.rating};return json({saved:true});}
      if(path.endsWith("/answers")){
        answerCalls++;assert(body.answer.trim());assert.equal(body.revision,session.revision);
        session={...session,answer:body.answer,revision:session.revision+1,status:"feedback",
          feedback:{result:"correct",feedback:"合成反馈：实际结果提供验证证据。",reference:"需要核对实际效果。",
            scheduled:!session.assisted,due_at:"2026-10-25T00:00:00Z"}};
        return json(session,202);
      }
      if(path.endsWith("/help")){
        session={...session,revision:session.revision+1,assisted:true,question:{...session.question,reference:"需要核对实际效果。"}};
        return json(session);
      }
      if(path.endsWith("/next")){session={...session,status:"finished",revision:session.revision+1};return json(session);}
      if(path==="/api/review/sessions/review-session")return json(session);
      throw Error("Unexpected review API "+method+" "+path);
    });
    await page.goto(served.origin+"/#/review");
    await page.getByRole("heading",{name:"我的复习",exact:true}).waitFor();
    await page.locator(".review-card").first().waitFor();
    assert.equal(await page.getByText(/待验证|待首次自测/).count(),0);
    assert.equal(await page.locator(".review-card").count(),2);
    await page.getByRole("button",{name:"查看合集",exact:true}).click();
    await page.getByRole("button",{name:"返回全部课程",exact:true}).waitFor();
    assert.equal(await page.locator(".review-card").count(),1);
    await page.getByRole("button",{name:"返回全部课程",exact:true}).click();
    await page.getByRole("searchbox",{name:"搜索复习课程"}).fill("不存在");
    await page.getByRole("button",{name:"清除筛选",exact:true}).click();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:`${output}/review-${width}.png`,fullPage:true});
    fail=true;
    await page.getByRole("button",{name:"刷新复习安排",exact:true}).click();
    await page.getByRole("button",{name:"重试读取",exact:true}).waitFor();
    fail=false;await page.getByRole("button",{name:"重试读取",exact:true}).click();
    await page.getByRole("button",{name:"开始今日复习",exact:true}).click();
    const dialog=page.getByRole("dialog",{name:"课程复习",exact:true});
    await dialog.getByLabel("你的回答",{exact:true}).waitFor();
    assert.equal(await dialog.getByRole("button",{name:"提交回答",exact:true}).isDisabled(),true);
    await dialog.getByLabel("你的回答",{exact:true}).fill("需要核对实际结果，不能仅靠自我评价。");
    await page.waitForFunction(()=>document.querySelector(".review-quiz")?.textContent.includes("草稿已保存"));
    assert(draft.answer.includes("实际结果"));
    await page.reload();
    await page.getByLabel("你的回答",{exact:true}).waitFor();
    assert.equal(await page.getByLabel("你的回答",{exact:true}).inputValue(),draft.answer);
    await page.getByRole("button",{name:"查看参考答案",exact:true}).click();
    await page.getByText("已查看参考，本题作为受助练习。",{exact:true}).waitFor();
    assert((await page.getByLabel("你的回答",{exact:true}).inputValue()).includes("实际结果"));
    await page.getByRole("button",{name:"提交回答",exact:true}).click();
    await page.getByText("本次未更新复习间隔。",{exact:true}).waitFor();
    assert.equal(answerCalls,1);
    await page.screenshot({path:`${output}/feedback-${width}.png`});
    await page.getByRole("button",{name:"下一题",exact:true}).click();
    await page.getByRole("heading",{name:"本轮复习结束",exact:true}).waitFor();
    await page.getByRole("button",{name:"返回复习安排",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector("dialog.review-quiz"));
    await page.close();
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({viewports:4,pageErrors:errors,scope:"production build, synthetic API only"}));
} finally {await browser.close();}
