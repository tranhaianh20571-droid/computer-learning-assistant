// 认证整页不得成为死胡同：四个认证路由各自都必须存在一条离开认证区的入口。
// 用法：TEST_FRONTEND=<url> PW_ROOT=<node_modules> TEST_BROWSER=<exe> node tests/auth_pages_browser.mjs
// 依赖：playwright-core（PW_ROOT 指向已安装的 node_modules）。不写入仓库证据目录以外的文件。
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import path from 'node:path';
const require=createRequire(path.resolve(process.env.PW_ROOT||'frontend/node_modules','package.json'));
const {chromium}=require('playwright-core');
const base=process.env.TEST_FRONTEND||'http://127.0.0.1:5173';
const AUTH=['/login','/register','/verify','/reset'],AUTH_SET=new Set(AUTH);
// 认证页内可离开的候选控件：跨页链接与 type=button（排除 type=submit，避免触发登录/注册提交）
const EXIT='.auth-col a[href^="#/"], .auth-col button[type="button"]';
const browser=await chromium.launch({executablePath:process.env.TEST_BROWSER,headless:true});
const page=await browser.newPage({viewport:{width:1280,height:900}}),checks=[],errors=[],dead=[];
page.on('pageerror',e=>errors.push(e.message));
try{
 for(const route of AUTH){
  await page.goto(`${base}/#${route}`,{waitUntil:'networkidle'});
  assert.equal(await page.locator('.auth-col').count(),1,`${route} 未渲染认证布局`);
  const total=await page.locator(EXIT).count();
  assert.ok(total>0,`${route} 没有任何可点击控件，认证表单本身可能未渲染`);
  let escaped=false;
  for(let i=0;i<total&&!escaped;i++){
   // 每个候选都从干净的认证页开始，避免上一次跳转污染 hash
   await page.goto(`${base}/#${route}`,{waitUntil:'networkidle'});
   await page.locator(EXIT).nth(i).click();
   await page.waitForTimeout(400);
   const hash=await page.evaluate(()=>location.hash);
   const target=hash.replace(/^#/,'');
   // 只有落在非空、且不属于认证区的路由才算真正离开
   if(target&&!AUTH_SET.has(target))escaped=true;
  }
  if(escaped)checks.push(`auth_exit_${route.slice(1)}`);else dead.push(route);
 }
 assert.deepEqual(dead,[],`认证页是死胡同，没有任何离开认证区的入口: ${dead.join(', ')}`);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({checks,deadEnds:dead,pageErrors:errors,synthetic:true}));
}finally{await browser.close();}
