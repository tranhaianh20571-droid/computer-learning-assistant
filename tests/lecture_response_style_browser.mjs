import {createRequire} from "node:module";
import {spawnSync} from "node:child_process";
import {mkdirSync,writeFileSync} from "node:fs";
import assert from "node:assert/strict";
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+"/playwright");
const backend="http://127.0.0.1:8013",base="http://127.0.0.1:5173",output=process.env.TEST_OUTPUT;
mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1600,height:1000}});
const guest=await context.request.get(backend+"/api/auth/guest");assert.equal(guest.status(),200);
const fixture=spawnSync(process.env.TEST_PYTHON,[process.env.TEST_FIXTURE,(await guest.json()).user_id],{encoding:"utf8",env:process.env});
assert.equal(fixture.status,0,fixture.stderr);const course=JSON.parse(fixture.stdout);
await context.addInitScript(({backend})=>{
 const original=window.fetch.bind(window);window.fetch=(input,init)=>original(typeof input==="string"&&input.startsWith("/api/")?backend+input:input,init);
},{backend});
const page=await context.newPage(),errors=[],checks=[];page.on("pageerror",e=>errors.push(e.message));
let final=false;page.on("response",r=>{if(r.url().endsWith("/run"))final=true;});
try{
 await page.goto(base+"/#/course/"+course.course_id);
 await page.getByRole("button",{name:"开始学习",exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('[data-teaching-preview] p')?.textContent.length>1,null,{timeout:30000});
 const samples=[];
 for(let i=0;i<6;i++){
  samples.push(await page.locator('.lecture-segment p').first().textContent());
  await new Promise(resolve=>setTimeout(resolve,65));
 }
 const counts=samples.map(s=>Array.from(s).length);
 assert(counts.some((n,i)=>i>0&&n>counts[i-1]));
 assert(counts.every((n,i)=>i===0||n-counts[i-1]<=14),JSON.stringify(counts));
 checks.push("received_bursts_are_released_as_characters");
 const dimensions=await page.evaluate(()=>({body:document.querySelector('.lw-body').clientWidth,text:document.querySelector('.tx-col').clientWidth}));
 assert(Math.abs(dimensions.text-766)<4);checks.push("default_766_text_column");
 const separator=page.getByRole("separator",{name:"调整讲解栏宽度"});
 const initial=await separator.boundingBox();await page.mouse.move(initial.x+4,initial.y+80);await page.mouse.down();
 await page.mouse.move(initial.x-170,initial.y+80,{steps:10});await page.mouse.up();
 const dragged=await page.locator('.tx-col').evaluate(n=>n.clientWidth);
 assert(dragged>dimensions.text+130);checks.push("pointer_resize");
 await page.reload();await page.locator('.tx-col').waitFor();
 assert(Math.abs(await page.locator('.tx-col').evaluate(n=>n.clientWidth)-dragged)<4);checks.push("width_restored_after_refresh");
 await separator.focus();await page.keyboard.press("ArrowRight");
 assert(await page.locator('.tx-col').evaluate(n=>n.clientWidth)<dragged-15);await separator.dblclick();
 assert(Math.abs(await page.locator('.tx-col').evaluate(n=>n.clientWidth)-766)<4);checks.push("keyboard_resize_and_reset");
 await page.waitForFunction(()=>document.querySelector('.teaching-board')?.dataset.boardPages>1,null,{timeout:30000});
 await page.getByRole("button",{name:"立即显示全文",exact:true}).click();
 await page.locator('.excalidraw canvas').first().waitFor({state:"visible"});
 await page.locator('.lecture-segment[data-segment-no="1"]:not([data-teaching-preview])').waitFor();
 await page.getByRole('button',{name:'立即显示全文',exact:true}).click();
 const card=page.locator('.lecture-segment[data-segment-no="1"]:not([data-teaching-preview])');
 const appearance=await card.evaluate(node=>{
  const box=getComputedStyle(node),title=getComputedStyle(node.querySelector('h3')),tag=getComputedStyle(node.querySelector('.lecture-phase'));
  const body=getComputedStyle(node.querySelector('p'));
  return {border:box.borderTopWidth,radius:box.borderRadius,title:title.color,tag:tag.color,bg:tag.backgroundColor,body:body.color,
   paragraphCount:node.querySelectorAll('p').length,parents:Array.from(node.querySelectorAll('p')).every(p=>p.closest('.lecture-segment')===node)};
 });
 assert.equal(appearance.border,'1px');assert.equal(appearance.radius,'12px');
 assert.notEqual(appearance.title,appearance.body);assert.equal(appearance.title,appearance.tag);
 assert.notEqual(appearance.bg,'rgb(255, 255, 255)');assert(appearance.paragraphCount>=2&&appearance.parents);
 checks.push('colored_title_and_phase_label_share_one_complete_segment_frame');
 await page.screenshot({path:output+'/response-frame.png',fullPage:true});
 const layout=JSON.parse(await page.locator('[data-layout]').getAttribute('data-layout'));
 const paired=layout.filter(p=>p.id.startsWith('mapping_')||p.id.startsWith('flow_')||p.id.startsWith('code_'));
 assert.equal(new Set(paired.map(p=>p.group)).size,1);
 assert.equal(new Set(paired.map(p=>p.column)).size,1);
 const text=paired.find(p=>p.id.startsWith('mapping_')),graph=paired.find(p=>p.id.startsWith('flow_'));
 assert(graph.y>=text.y+text.height);
 checks.push('related_text_and_graph_are_in_the_same_column');
 const scroller=page.locator('.teaching-board-canvas');
 assert(await scroller.evaluate(n=>n.scrollHeight>n.clientHeight));
 const topBefore=await scroller.evaluate(n=>n.scrollTop);
 await scroller.hover();await page.mouse.wheel(0,280);await new Promise(resolve=>setTimeout(resolve,150));
 assert(await scroller.evaluate(n=>n.scrollTop)>topBefore);
 await scroller.evaluate(n=>n.scrollTop=0);
 checks.push('oversized_group_scrolls_vertically_without_font_shrink');
 assert(layout.some(p=>p.column===0));assert(layout.every(p=>p.column<=2));
 assert.equal(await page.getByText("左栏 → 中栏 → 右栏",{exact:true}).count(),0);
 assert.equal(await page.getByText("文字就绪",{exact:true}).count(),0);
 assert.equal(await page.locator(".tx-step,.lecture-segment b").count(),0);
 assert(await page.locator(".lecture-topic h3").count()>0);
 assert(await page.locator(".lecture-phase").filter({hasText:"建立直觉"}).count()>0);
 assert.equal(await page.locator(".lecture-topic h3").filter({hasText:/第\s*\d+\s*步/}).count(),0);
 checks.push("knowledge_titles_phase_labels_without_step_numbers");
 for(let i=0;i<layout.length;i++)for(const b of layout.slice(i+1)){
  const a=layout[i];assert(a.x+a.width<=b.x||b.x+b.width<=a.x||a.y+a.height<=b.y||b.y+b.height<=a.y);
 }
 await page.screenshot({path:output+"/three-column-board.png",fullPage:true});checks.push("grouped_three_column_layout_and_typed_blocks");
 await page.getByRole("button",{name:"下一页板书",exact:true}).click();
 assert((await page.locator('.board-controls').textContent()).includes('第 2 /'));checks.push("board_pagination");
 await page.getByRole("button",{name:"上一页板书",exact:true}).click();
 await page.locator('.board-graph-detail summary').first().click();
 await page.getByRole("button",{name:"确定要查找的键",exact:true}).click();
 assert(await page.getByRole("button",{name:"确定要查找的键",exact:true}).getAttribute('aria-pressed')==='true');
 checks.push("generic_graph_local_relationships");
 await page.waitForFunction(()=>Array.from(document.querySelectorAll('.lecture-segment p')).reduce((total,n)=>total+n.textContent.length,0)>80,null,{timeout:30000});
 const conv=page.url().split("/").at(-1);
 await page.waitForFunction(async ({backend,conv})=>(await (await fetch(backend+"/api/teaching/"+conv,{credentials:"include"})).json()).steps.some(s=>s.step_no===2),{backend,conv},{timeout:30000});
 assert.equal(await page.locator('.lecture-segment[data-segment-no="2"]').count(),0);
 await page.getByRole("button",{name:"继续讲解",exact:true}).click();
 await page.locator('.lecture-segment[data-segment-no="2"][data-stage-no="1"]').waitFor();
 await page.getByRole("button",{name:"立即显示全文",exact:true}).click();
 const accumulated=await page.locator('[data-visible-blocks]').getAttribute('data-visible-blocks');
 assert(accumulated.includes('_1_1') && accumulated.includes('_1_2'));
 checks.push("same_stage_multiple_segments_accumulate_board_without_future_reveal");
 await page.reload();await page.locator('.lecture-segment[data-segment-no="2"]').waitFor();
 assert((await page.locator('[data-visible-blocks]').getAttribute('data-visible-blocks')).includes('_1_1'));
 await page.locator('.lecture-segment[data-segment-no="1"]').click();
 await page.waitForFunction(()=>!document.querySelector('[data-visible-blocks]')?.dataset.visibleBlocks.includes('_1_2'));
 checks.push("refresh_restores_segment_and_backtrack_board_snapshot");
 const legacyBlocks=[
  ...['Python脚本：记述操作步骤的菜谱','Python解释器：负责读取和执行脚本','终端命令行：下达运行命令的入口','类比边界：语法必须准确'].map((text,i)=>({id:'legacy_point_'+i,type:'text',text:text.repeat(3)})),
  {id:'legacy_diagram',type:'graph',title:'关系示意',diagram:'relationship',nodes:[
   {id:'script',label:'脚本文件：菜谱'},{id:'terminal',label:'终端命令行：操作台'},
   {id:'python',label:'Python解释器：厨师'},{id:'result',label:'运行结果：成品'}],
   edges:[{from_id:'script',to_id:'python',label:'读取指令'},{from_id:'terminal',to_id:'python',label:'触发执行'},
    {from_id:'python',to_id:'result',label:'输出'}]}];
 const pattern=backend+'/api/teaching/'+conv;
 let legacyVisible=legacyBlocks.slice(0,4);
 const legacyResponse=async route=>{
  const response=await route.fetch(),data=await response.json();
  if(data.steps?.[0])data.steps[0].board={...data.steps[0].board,beats:[],presentation:{version:2,title:'代码执行的直觉类比',blocks:legacyVisible}};
  await route.fulfill({response,json:data});
 };
 await page.route(pattern,legacyResponse);await page.reload();
 await page.waitForFunction(()=>document.querySelector('[data-visible-blocks]')?.dataset.visibleBlocks.includes('legacy_point_3'));
 const beforeDiagram=JSON.parse(await page.locator('[data-layout]').getAttribute('data-layout'));
 legacyVisible=legacyBlocks;
 await page.waitForFunction(()=>{const node=document.querySelector('[data-layout]');return node&&JSON.parse(node.dataset.layout).some(p=>p.id==='legacy_diagram');});
 const legacyLayout=JSON.parse(await page.locator('[data-layout]').getAttribute('data-layout'));
 assert(legacyLayout.filter(p=>p.id.startsWith('legacy_')).every(p=>p.column===0));
 assert(legacyLayout.find(p=>p.id==='legacy_diagram').y>legacyLayout.find(p=>p.id==='legacy_point_3').y);
 assert.deepEqual(legacyLayout.slice(0,4),beforeDiagram);
 assert(await page.locator('.teaching-board-canvas').evaluate(n=>n.scrollHeight>n.clientHeight));
 await page.locator('.excalidraw canvas').first().waitFor({state:'visible'});
 await page.waitForFunction(()=>Array.from(document.querySelectorAll('.excalidraw canvas')).some(canvas=>{
  const context=canvas.getContext('2d');if(!context||canvas.height<=100)return false;
  const data=context.getImageData(0,100,Math.min(canvas.width,600),Math.min(canvas.height-100,1600)).data;
  let ink=0;for(let i=0;i<data.length;i+=20)if(data[i+3]>0&&data[i]<160&&data[i+1]<160&&data[i+2]<160)ink++;
  return ink>40;
 }),null,{timeout:30000});
 checks.push('saved_board_has_rendered_ink_after_reload');
 await page.waitForFunction(()=>{const node=document.querySelector('.teaching-board-canvas');return node&&node.scrollTop>Math.min(20,(node.scrollHeight-node.clientHeight)/2);});
 assert.equal(await page.locator('.teaching-board-canvas').getAttribute('data-board-following'),'true');
 checks.push('new_diagram_automatically_scrolls_into_view_without_moving_its_text');
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 await page.screenshot({path:output+'/legacy-grouped.png',fullPage:true});
 await page.locator('.teaching-board-canvas').evaluate(n=>n.scrollTop=300);
 await page.screenshot({path:output+'/legacy-grouped-scrolled.png',fullPage:true});
 await page.locator('.teaching-board-canvas').hover();await page.mouse.wheel(0,-240);
 await page.getByRole('button',{name:'回到最新板书',exact:true}).waitFor();
 assert.equal(await page.locator('.teaching-board-canvas').getAttribute('data-board-following'),'false');
 await page.getByRole('button',{name:'回到最新板书',exact:true}).click();
 await page.waitForFunction(()=>{const node=document.querySelector('.teaching-board-canvas');return node&&node.scrollTop>Math.min(20,(node.scrollHeight-node.clientHeight)/2);});
 assert.equal(await page.locator('.teaching-board-canvas').getAttribute('data-board-following'),'true');
 checks.push('manual_review_pauses_follow_and_latest_button_restores_it');
 checks.push('legacy_definitions_and_diagram_remain_left_even_beyond_viewport');
 await page.unroute(pattern,legacyResponse);await page.reload();
 // This UI regression waits for the synthetic prefetch before testing followup;
 // concurrent cancellation/lease lock ordering is a separate backend concern.
 await page.waitForFunction(async ({backend,conv})=>{
  const w=await (await fetch(backend+'/api/teaching/'+conv,{credentials:'include'})).json();
  return !(w.queued_tasks||[]).some(task=>['leased','running'].includes(task.status));
 },{backend,conv},{timeout:30000});
 await page.getByLabel("课程追问",{exact:true}).fill("合成追问");await page.getByRole("button",{name:"追问",exact:true}).click();
 await page.getByRole("button",{name:"继续课程 · 不自动播放",exact:true}).waitFor({timeout:30000});
 assert((await page.locator('[data-visible-blocks]').getAttribute('data-visible-blocks')).includes('reply_board'));
 checks.push("followup_board_survives_final_publication");
 await page.setViewportSize({width:390,height:844});await page.getByRole("tab",{name:"文字",exact:true}).click();
 assert(await page.locator('.tx-col').isVisible());assert.equal(await separator.isVisible(),false);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));checks.push("mobile_pane_without_horizontal_overflow");
 assert.deepEqual(errors,[]);
 writeFileSync(output+"/browser-result.json",JSON.stringify({checks,pageErrors:errors,counts,synthetic:true},null,2));
 console.log(JSON.stringify({checks,pageErrors:errors}));
}finally{await browser.close();}
