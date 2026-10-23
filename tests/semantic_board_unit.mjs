import assert from "node:assert/strict";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
import {presentBoard} from "../frontend/src/lib/boardPresentation.ts";

const objects=[
  {id:"array",type:"array",title:"搜索范围",cells:Array.from({length:8},(_,i)=>({id:`a${i}`,value:String(i*4),index:String(i),state:i===4?"focus":i<4?"discarded":"normal"})),note:"保留右半段"},
  {id:"stack",type:"stack",frames:[{id:"f1",label:"fact(1)",variables:["n = 1"],waiting_for:"返回 1",active:true},{id:"f2",label:"fact(2)",variables:["n = 2"],waiting_for:"等待返回"}]},
  {id:"layers",type:"layers",layers:[{id:"h",label:"HTML",detail:"内容与结构"},{id:"c",label:"CSS",detail:"样式与布局"}]},
  {id:"graph",type:"graph",diagram:"flowchart",nodes:[{id:"p",label:"执行"},{id:"d",label:"是否匹配？",shape:"decision"}],edges:[{from_id:"p",to_id:"d",label:"判断"}]},
  {id:"seq",type:"graph",diagram:"sequence",nodes:[{id:"a",label:"浏览器"},{id:"b",label:"服务"}],edges:[{from_id:"a",to_id:"b",label:"请求"}]},
];
for(const width of [320,390,768,1440]){
  for(const block of objects){
    const geometry=layoutBoard([block],width,new Set([block.id]),[],{},"合成板书",{},{responsive:true,theme:"ink"});
    const page=geometry.pages[0];
    assert(page.elements.every(item=>Number.isFinite(item.x)&&Number.isFinite(item.y)));
    assert(page.placements.every(item=>item.x>=0&&item.x+item.width<=geometry.width));
    assert(page.elements.some(item=>item.type==="text"&&item.strokeColor==="#f1f3e9"));
    assert(!page.elements.some(item=>["#eef3ef","#dce9f8"].includes(item.backgroundColor)),"light graph surfaces must not retain chalk text");
    if(["array","stack","layers"].includes(block.type)){
      const partial=presentBoard(page,[{...block,reveal_progress:.2}]);
      const full=presentBoard(page,[{...block,reveal_progress:1}]);
      assert(partial.length<full.length,"semantic objects reveal incrementally without reflow");
    }
    if(block.id==="graph")assert(page.elements.some(item=>item.type==="diamond"));
  }
}
console.log("PASS: semantic arrays/stacks/layers, flow shapes, sequence contrast and incremental geometry at four widths");
