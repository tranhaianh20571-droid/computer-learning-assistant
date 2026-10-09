import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdirSync} from "node:fs";
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+"/playwright");
const base=process.env.TEST_FRONTEND||"http://127.0.0.1:5173";
const long=String.raw`T(n)=O(g(n))\iff\exists c>0,n_0>0,\forall n\ge n_0:T(n)\le c\cdot g(n)`;
const atomic=String.raw`\frac{\text{${"A long indivisible numerator kept available for horizontal scrolling. ".repeat(3)}}}{1+x^2}`;
const blocks=[
  {id:"long",type:"formula",title:"Synthetic formula title",formula:long},
  {id:"after",type:"text",title:"Following topic",text:"Synthetic following content."},
  {id:"fraction",type:"formula",title:"Fraction",formula:String.raw`\frac{1}{1+\frac{x^2}{y}}`},
  {id:"atomic",type:"formula",title:"Indivisible formula",formula:atomic},
  {id:"aligned",type:"formula",title:"Aligned rows",formula:String.raw`\begin{aligned}a&=\frac{1}{x}\\b&=a^2\\c&=a+b\end{aligned}`},
  {id:"plain",type:"formula",title:"Plain formula",format:"plain",formula:"x plus y",plain_text:"x + y"},
  {id:"invalid",type:"formula",title:"Fallback",formula:String.raw`\htmlClass{bad}{x}`},
  ...Array.from({length:8},(_,i)=>({id:`page${i}`,type:"formula",title:`Pagination ${i}`,span:3,
    formula:String.raw`\begin{aligned}a&=1\\b&=2\\c&=3\end{aligned}`}))
];
const scene={presentation:{blocks,all_blocks:blocks,title:"Synthetic formula board"}};
const browser=await chromium.launch({headless:true,...(process.env.TEST_BROWSER?{executablePath:process.env.TEST_BROWSER}:{})});
try {
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage(),errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    page.on("console",message=>{if(/Maximum update depth/.test(message.text()))errors.push(message.text());});
    // Preserve Vite's refresh preamble; replace only the application entry with a synthetic board.
    await page.route('**/src/main.tsx*',route=>route.fulfill({contentType:"text/javascript",body:`
      import React from '/node_modules/.vite/deps/react.js';
      import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
      import TeachingBoard from '/src/ui/TeachingBoard.tsx';
      import '/src/tokens.css';
      const root=ReactDOM.createRoot(document.getElementById('root'));
      window.setBoard=scene=>root.render(React.createElement(React.StrictMode,null,React.createElement(TeachingBoard,{scene,sceneKey:'fixture'})));
      window.setBoard(${JSON.stringify(scene)});
    `}));
    await page.goto(base);
    await page.locator('.excalidraw canvas').first().waitFor({state:'visible'});
    await page.locator('[data-formula-id=long] .katex').waitFor({state:'visible'});
    await page.evaluate(()=>document.fonts.ready);
    await page.waitForTimeout(350);
    await page.waitForFunction(()=>{
      const canvas=document.querySelector('.excalidraw canvas');
      const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
      let ink=0;for(let i=0;i<data.length;i+=4)if(data[i+3]&&data[i]<160&&data[i+1]<160&&data[i+2]<160)ink++;
      return ink>100;
    });
    const checked=new Set();
    const pages=Number(await page.locator('.teaching-board').getAttribute('data-board-pages'));
    assert(pages>1,'fixture must exercise pagination');
    for(let p=0;p<pages;p++){
      const placements=JSON.parse(await page.locator('[data-layout]').getAttribute('data-layout'));
      for(const placement of placements){
        if(!placement.formulaBody)continue;
        const id=placement.id,overlay=page.locator(`[data-formula-id="${id}"]`),content=overlay.locator('.teaching-formula-content');
        await overlay.waitFor();
        const scale=Number(await page.locator('[data-board-scale]').getAttribute('data-board-scale'));
        const rect=await overlay.boundingBox(),sheet=await page.locator('.teaching-board-sheet').boundingBox();
        assert(Math.abs(rect.y-sheet.y-(placement.formulaBody.y+10)*scale)<2,'body uses its own scene coordinates');
        assert(placement.formulaBody.y>placement.y,'title occupies a separate area');
        const dimensions=await content.evaluate(node=>({height:node.offsetHeight,scrollWidth:node.scrollWidth,clientWidth:node.clientWidth}));
        assert(dimensions.height<=placement.formulaBody.height+1,'measured height contains formula and scrollbar');
        checked.add(id);
        if(id==='long'){
          if(width>=640)assert(placement.span>1,'long formula expands across columns');
          assert(dimensions.scrollWidth<=dimensions.clientWidth+2,'semantic KaTeX bases wrap within available width');
          if(width===390)assert(rect.x+rect.width<width,'long formula wraps inside the mobile viewport');
          const after=placements.find(item=>item.id==='after');
          if(after&&after.x===placement.x)assert(after.y>=placement.y+placement.height);
        }
        if(id==='atomic'){
          assert(dimensions.scrollWidth>dimensions.clientWidth,'indivisible formula remains scrollable');
          await content.evaluate(node=>node.scrollLeft=node.scrollWidth);
          assert(await content.evaluate(node=>node.scrollLeft>0));
          await content.evaluate(node=>node.scrollLeft=0);
        }
        if(id==='plain'||id==='invalid')assert.equal(await content.locator('.katex').count(),0);
      }
      if(p===0){
        const oldScale=Number(await page.locator('[data-board-scale]').getAttribute('data-board-scale'));
        await page.getByRole('button',{name:'放大板书',exact:true}).click();
        await page.waitForFunction(scale=>Number(document.querySelector('[data-board-scale]').dataset.boardScale)>scale,oldScale,{timeout:5000});
        await page.getByRole('button',{name:'适应画面',exact:true}).click();
        await page.waitForFunction(scale=>Math.abs(Number(document.querySelector('[data-board-scale]').dataset.boardScale)-scale)<.01,oldScale,{timeout:5000});
        if(process.env.TEST_OUTPUT){mkdirSync(process.env.TEST_OUTPUT,{recursive:true});await page.screenshot({path:`${process.env.TEST_OUTPUT}/formula-${width}.png`});}
      }
      if(p<pages-1)await page.getByRole('button',{name:'下一页板书',exact:true}).click();
    }
    assert.equal(checked.size,blocks.filter(b=>b.type==='formula').length);
    const revisions=await page.locator('.teaching-board').getAttribute('data-layout-revisions');
    await page.waitForTimeout(500);
    assert.equal(await page.locator('.teaching-board').getAttribute('data-layout-revisions'),revisions,'measurements must settle');
    await page.evaluate(scene=>window.setBoard({...scene,presentation:{...scene.presentation,blocks:[]}}),scene);
    await page.waitForFunction(()=>!document.querySelector('.teaching-formula-overlay'));
    await page.reload();
    await page.locator('[data-formula-id=long] .katex').waitFor();
    assert.deepEqual(errors,[]);
    console.log(`Formula board ${width}px: body separation, measured height, spans/wrapping, horizontal scroll, fallback, zoom, ${pages} pages, reveal and reload passed`);
    await context.close();
  }
}finally{await browser.close();}
