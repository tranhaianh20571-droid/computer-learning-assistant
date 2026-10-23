/** Every reveal stage introduces a complete relationship and its endpoints. */
export function graphStages(block:any,positions?:Record<string,{x:number;y:number}>){
  const nodes=block.nodes||[],edges=block.edges||[];
  const nodeStages:Record<string,number>={},edgeStages:number[]=[];
  if(positions){
    const ordered=[...nodes].sort((a,b)=>positions[a.id].y-positions[b.id].y||positions[a.id].x-positions[b.id].x);
    let stage=-1,lastY=-Infinity;
    for(const node of ordered){
      const y=positions[node.id].y;
      if(y-lastY>1){stage++;lastY=y;}
      nodeStages[node.id]=stage;
    }
    edges.forEach((edge:any,index:number)=>{
      edgeStages[index]=Math.max(nodeStages[edge.from_id]??0,nodeStages[edge.to_id]??0);
    });
    return {nodeStages,edgeStages,total:Math.max(1,stage+1)};
  }
  let stage=0;
  const roots=nodes.filter((node:any)=>!edges.some((edge:any)=>edge.to_id===node.id));
  const initial=roots[0]||nodes[0];
  if(initial)nodeStages[initial.id]=stage++;
  const pending=new Set<number>(edges.map((_:any,i:number)=>i));
  while(pending.size){
    const index=[...pending].find(i=>nodeStages[edges[i].from_id]!==undefined)??[...pending][0];
    const edge=edges[index];
    nodeStages[edge.from_id]??=stage;
    nodeStages[edge.to_id]??=stage;
    edgeStages[index]=stage++;
    pending.delete(index);
  }
  for(const node of nodes)if(nodeStages[node.id]===undefined)nodeStages[node.id]=stage++;
  return {nodeStages,edgeStages,total:Math.max(1,stage)};
}
