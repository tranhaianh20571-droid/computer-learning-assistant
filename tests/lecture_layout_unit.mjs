import assert from "node:assert/strict";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
const points=[
 "Python 脚本（.py）：记述操作步骤的菜谱",
 "Python 解释器：负责翻译并执行菜谱的厨师",
 "终端命令行：下达运行命令的后厨操作台",
 "类比边界：解释器极度精确，对语法错误零容忍",
].map((text,i)=>({id:"p"+i,type:"text",text}));
const graph={id:"execution",type:"graph",title:"关系示意",span:1,diagram:"relationship",
 nodes:[{id:"script",label:"脚本文件（.py）[菜谱]"},{id:"terminal",label:"终端命令行 [操作台呼叫]"},
 {id:"python",label:"Python 解释器 [大厨执行]"},{id:"result",label:"运行结果 [成品菜肴]"}],
 edges:[{from_id:"script",to_id:"python",label:"读取指令"},{from_id:"terminal",to_id:"python",label:"触发执行"},
 {from_id:"python",to_id:"result",label:"输出"}]};
const blocks=[...points,graph];
const layout=layoutBoard(blocks,1280,new Set(blocks.map(b=>b.id)));
const place=layout.pages[0].placements.find(p=>p.id==="execution");
assert.equal(place.column,0,"compact graph should use left-column remainder");
const elements=layout.pages[0].elements;
const label=elements.find(e=>e.customData?.edgeFrom==="script" && e.type==="text");
assert.equal(label.text,"读取指令");
const target=elements.find(e=>e.id==="execution_node_python");
assert(label.y<target.y+target.height,"skip-edge caption should be alongside the route, not below the diagram");
assert(elements.filter(e=>e.id.startsWith("execution_node_text_")).every(e=>e.fontSize>=12&&e.fontSize<=14));
for(const edge of graph.edges){
 const captions=elements.filter(e=>e.type==="text"&&e.customData?.edgeFrom===edge.from_id&&e.customData?.edgeTo===edge.to_id);
 assert.equal(captions.length,1);assert.equal(captions[0].text.replaceAll("\n",""),edge.label);
 assert(captions[0].x+captions[0].width<=place.x+place.width);
}
console.log("Legacy example packs into left column at readable font; each edge has an attached caption");
