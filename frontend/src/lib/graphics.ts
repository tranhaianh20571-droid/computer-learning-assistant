import {parse} from "mathjs";

export type Bounds={x:[number,number];y:[number,number]};
export type Point={x:number;y:number;label?:string};
export type Curve={expression:string;label?:string};
export type GateKind="NOT"|"AND"|"OR"|"XOR"|"NAND"|"NOR"|"XNOR"|"BUFFER";
export type Gate={id:string;type:GateKind;x:number;y:number};
export type Wire={source:string;target:string;port:number};
export type Circuit={kind:"circuit";inputs:string[];outputs:string[];gates:Gate[];wires:Wire[]};
export type Plot={kind:"plot";bounds:Bounds;curves:Curve[];points:Point[]};
export type Surface={kind:"surface";bounds:Bounds;expression:string};
export type Graphic=Plot|Surface|Circuit;
export type PlotAnswer={kind:"plot";curves:Curve[];points:Point[];strokes:Point[][]};
export type DrawingAnswer=PlotAnswer|Circuit;
export type DrawingSpec=
  {kind:"plot";bounds:Bounds;starter?:Plot|null;allow_expression:boolean;allow_sketch:boolean;allow_points:boolean}|
  {kind:"circuit";inputs:string[];outputs:string[];allowed_gates:GateKind[];starter?:Circuit|null};

/** Only whitelisted math ASTs reach mathjs compilation; no assignments or accessors. */
export function expression(source:string,variables=["x"]):(scope:Record<string,number>)=>number{
  if(!source||source.length>240)throw Error("表达式长度不正确");
  const ast=parse(source.replaceAll("**","^"));
  const functions=new Set(["sin","cos","tan","sqrt","exp","log","abs"]);
  let count=0;
  function check(node:any,depth=0){
    if(++count>96||depth>16)throw Error("表达式过于复杂");
    if(node.isConstantNode){if(typeof node.value!=="number"||!Number.isFinite(node.value)||Math.abs(node.value)>1e6)throw Error("数值超出范围");}
    else if(node.isSymbolNode){if(![...variables,"pi","e"].includes(node.name))throw Error("表达式包含未知变量");}
    else if(node.isParenthesisNode)check(node.content,depth+1);
    else if(node.isOperatorNode&&["add","subtract","multiply","divide","pow","unaryMinus","unaryPlus"].includes(node.fn)){
      if(node.implicit)throw Error("乘法请使用 *");
      if(node.fn==="pow"){
        let exponent=node.args[1];
        while(exponent.isParenthesisNode)exponent=exponent.content;
        if(exponent.isOperatorNode&&["unaryMinus","unaryPlus"].includes(exponent.fn))exponent=exponent.args[0];
        if(!exponent.isConstantNode||Math.abs(exponent.value)>12)throw Error("幂指数须为不超过12的常数");
      }
      node.args.forEach((a:any)=>check(a,depth+1));
    }else if(node.isFunctionNode&&node.fn.isSymbolNode&&functions.has(node.fn.name)&&node.args.length===1)check(node.args[0],depth+1);
    else throw Error("不支持的表达式");
  }
  check(ast);const compiled=ast.compile();
  return scope=>{try{const value=compiled.evaluate(scope);return typeof value==="number"&&Number.isFinite(value)&&Math.abs(value)<=1e12?value:NaN;}catch{return NaN;}};
}

export function emptyDrawing(spec:DrawingSpec):DrawingAnswer{
  if(spec.kind==="circuit")return spec.starter?structuredClone(spec.starter):{kind:"circuit",inputs:spec.inputs,outputs:spec.outputs,gates:[],wires:[]};
  return {kind:"plot",curves:structuredClone(spec.starter?.curves||[]),points:structuredClone(spec.starter?.points||[]),strokes:[]};
}
export function hasDrawing(value:unknown):value is DrawingAnswer{
  if(!value||typeof value!=="object")return false;
  const v=value as DrawingAnswer;
  return v.kind==="circuit"?Array.isArray(v.gates)&&Array.isArray(v.wires)&&Boolean(v.gates.length||v.wires.length):
    v.kind==="plot"&&Array.isArray(v.curves)&&Array.isArray(v.points)&&Array.isArray(v.strokes)&&Boolean(v.curves.length||v.points.length||v.strokes.length);
}
export function circuitIssues(value:Circuit){
  const issues:string[]=[],seen=new Set<string>(),visiting=new Set<string>();
  const gates=new Map(value.gates.map(g=>[g.id,g]));
  const drivers=new Map(value.wires.map(w=>[`${w.target}:${w.port}`,w.source]));
  let missing=0;
  for(const id of [...value.gates.map(g=>g.id),...value.outputs]){
    const gate=gates.get(id),ports=gate&&!["NOT","BUFFER"].includes(gate.type)?2:1;
    for(let p=0;p<ports;p++)if(!drivers.has(`${id}:${p}`))missing++;
  }
  if(missing)issues.push(`${missing} 个输入引脚未连接`);
  const visit=(id:string):boolean=>{
    if(visiting.has(id))return true;
    if(seen.has(id))return false;
    visiting.add(id);
    const loop=value.wires.filter(w=>w.target===id).some(w=>visit(w.source));
    visiting.delete(id);seen.add(id);return loop;
  };
  if(value.gates.some(g=>visit(g.id)))issues.push("存在反馈环");
  return issues;
}

export function circuitGeometry(circuit:Circuit){
  const width=Math.max(760,...circuit.gates.map(g=>g.x+190));
  const height=Math.max(280,...circuit.gates.map(g=>g.y+110),circuit.inputs.length*66+26,circuit.outputs.length*66+26);
  const pin=(id:string,port=0,output=false):[number,number]=>{
    const i=circuit.inputs.indexOf(id);if(i>=0)return[42,42+i*66];
    const o=circuit.outputs.indexOf(id);if(o>=0)return[width-42,42+o*66];
    const g=circuit.gates.find(g=>g.id===id);if(!g)return[0,0];
    if(output)return[g.x+76,g.y+28];
    return[g.x, g.y+(["NOT","BUFFER"].includes(g.type)?28:port===0?16:40)];
  };
  return{width,height,pin};
}
