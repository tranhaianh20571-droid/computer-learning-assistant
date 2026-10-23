import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,readFile} from "node:fs/promises";
import nodePath from "node:path";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const actual=process.env.TEST_CHARACTER_MANIFEST?JSON.parse(await readFile(process.env.TEST_CHARACTER_MANIFEST,"utf8")):null;
const output=".backup/character-media-browser"+(actual?"/actual":"");
await mkdir(output,{recursive:true});
try{
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:1000},serviceWorkers:"block"});
  const page=await context.newPage(),errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  const served=await mockBuiltFrontend(page);
  let assets=[
   ...["neutral","explaining","thinking"].map(scene=>({asset_id:scene,scene,image_part:"portrait"})),
   {asset_id:"upper",scene:"neutral",image_part:"upper_arm"},
   {asset_id:"lower",scene:"neutral",image_part:"forearm"},
   {asset_id:"body",scene:"neutral",image_part:"pointing_body"},
  ].map(asset=>({...asset,kind:"image",confirmed:true,url:"/api/cards/mentor/assets/"+asset.asset_id}));
  let profile={name:"Synthetic tutor",identity:"",background:"",personality:"",appearance:"",tags:[],
   voice_id:"",voice_config_id:"",voice_selection:"auto",speaking_style:"",media_ids:assets.map(a=>a.asset_id),
   base_asset_id:"neutral",pointing_rig:{upper_arm_asset_id:"upper",forearm_asset_id:"lower",
    body_asset_id:"body",reference_asset_id:"neutral",
    shoulder_x:.24,shoulder_y:.43,upper_length:.27,forearm_length:.58}};
  if(actual){
   assets=actual.assets.filter(asset=>actual.profile.media_ids.includes(asset.asset_id));
   profile=actual.profile;
  }
  const card={card_id:"mentor",revision:1,editable:true,assets,profile};
  const json=(route,body)=>route.fulfill({contentType:"application/json",body:JSON.stringify(body)});
  await page.route("**/api/**",async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved",is_admin:false});
   if(path==="/api/market/profile")return json(route,{display_name:"合成用户"});
   if(path==="/api/market/imports/drain")return json(route,{cards:[]});
   if(path==="/api/mentor-preference")return json(route,{version:0,card_id:null});
   if(path==="/api/cards")return json(route,{data:[card]});
   if(path==="/api/cards/mentor")return json(route,card);
   if(path.endsWith("/media-jobs"))return json(route,{data:[
    {task_id:"old-success",kind:"image",status:"succeeded",assets:[]},
    {task_id:"old-failed",kind:"image",status:"failed",assets:[]},
   ]});
   if(path==="/api/capabilities/configs")return json(route,{data:[]});
   if(path==="/api/character-expression")return json(route,{data:[],recommended_voice_id:null,
    compatible_voice_ids:[],match_status:"no_compatible_voice",voice_gender:"unknown",speaking_style:""});
   if(path.includes("/assets/")){
    if(actual)return route.fulfill({contentType:"image/png",body:await readFile(nodePath.join(
     nodePath.dirname(process.env.TEST_CHARACTER_MANIFEST),path.split("/").at(-1)+".png"))});
    const arm=path.endsWith("/upper")||path.endsWith("/lower");
    return route.fulfill({contentType:"image/svg+xml",body:arm?
     '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="40"><rect width="300" height="40" fill="#a65a36"/></svg>':
     '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><circle cx="150" cy="80" r="40" fill="#e6a46d"/><rect x="90" y="120" width="120" height="180" fill="#293d30"/></svg>'});
   }
   throw new Error("Unexpected synthetic API "+path);
  });
  await page.goto(served.origin+"/#/card/mentor");
  const gallery=page.locator(".character-details > .character-section").filter({has:page.getByRole("heading",{name:"形象与动作",exact:true})}).locator(":scope > .character-media-grid");
  await gallery.locator(".character-media").waitFor();
  assert.equal(await gallery.locator(".character-media").count(),1);
  assert.equal(await page.getByLabel("候选数量",{exact:true}).count(),0);
  assert.equal(await page.locator(".character-job").count(),0);
  assert.equal(await page.getByRole("button",{name:"重新生成",exact:true}).count(),1);
  const scene=page.getByLabel("场景",{exact:true});
  await scene.selectOption("explaining");
  assert.equal(await gallery.locator(".character-media").count(),1);
  assert((await gallery.innerText()).includes("讲解"));
  assert(!(await gallery.innerText()).includes("自然站姿"));
  await scene.selectOption("thinking");
  assert((await gallery.innerText()).includes("思考"));
  assert.equal(await page.getByText("指向素材",{exact:true}).count(),0);
  assert.equal(await page.locator(".pointing-preview,.character-rig,.character-rig-settings").count(),0);
  assert.equal(await page.locator('img[src$="/upper"],img[src$="/lower"],img[src$="/body"]').count(),0);
  await page.screenshot({path:`${output}/preview-${width}.png`,fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
  await page.goto(served.origin+"/#/cards");
  await page.locator(".character-cover img").waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll(".character-cover img")]
    .every(image=>image.complete&&image.naturalWidth>0));
  assert(await page.evaluate(()=>[...document.querySelectorAll(".character-tile")].every(tile=>{
    const cover=tile.querySelector(".character-cover").getBoundingClientRect();
    const image=tile.querySelector(".character-cover img").getBoundingClientRect();
    const text=tile.querySelector(".character-tile-meta").getBoundingClientRect();
    return image.bottom<=cover.bottom+1&&image.top>=cover.top-1&&cover.bottom<=text.top+1;
  })));
  await page.screenshot({path:`${output}/library-${width}.png`,fullPage:true});
  assert.deepEqual(errors,[]);assert.deepEqual(served.unexpected,[]);
  await context.close();
 }
 console.log("Scene filtering, legacy arm assets hidden, complete portraits: desktop/mobile passed");
}finally{await browser.close();}
