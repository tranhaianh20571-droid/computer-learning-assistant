/** Reveal the active writing area, preserving manual review and the chosen zoom. */
export function followTarget(top:number,height:number,viewport:number,position:number,total:number){
  const margin=18;
  const bottom=top+height;
  if(top>=position+margin && bottom<=position+viewport-margin)return position;
  const target=height>viewport-2*margin?top-margin:bottom-viewport+margin;
  return Math.max(0,Math.min(target,Math.max(0,total-viewport)));
}

/** Keep the writing line in a central band; only user navigation suspends this. */
export function writingTarget(y:number,viewport:number,position:number,total:number,force=false){
  const relative=y-position;
  if(!force&&relative>=viewport*.4&&relative<=viewport*.6)return position;
  return Math.max(0,Math.min(y-viewport/2,Math.max(0,total-viewport)));
}
