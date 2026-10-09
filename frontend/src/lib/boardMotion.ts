export type MotionViewport = {scrollTop:number;clientHeight:number;scrollHeight:number};
export type MotionClock = {request:(callback:(time:number)=>void)=>number;cancel:(id:number)=>void};

/** One critically damped trajectory; new destinations preserve its velocity. */
export class BoardMotion {
  private frame:number|null=null;
  private time:number|null=null;
  private velocity=0;
  private position:number|null=null;
  private target=0;
  private reduced=false;
  private node:MotionViewport;
  private clock:MotionClock;
  private onActive:(active:boolean)=>void;
  constructor(node:MotionViewport,clock:MotionClock,onActive:(active:boolean)=>void=()=>{}){
    this.node=node;this.clock=clock;this.onActive=onActive;
  }
  get active(){return this.frame!==null;}
  get destination(){return this.target;}
  private clamp(value:number){return Math.max(0,Math.min(value,Math.max(0,this.node.scrollHeight-this.node.clientHeight)));}
  stop(){
    if(this.frame!==null)this.clock.cancel(this.frame);
    this.frame=null;this.time=null;this.velocity=0;this.position=null;this.onActive(false);
  }
  setReduced(reduced:boolean){
    this.reduced=reduced;
    if(reduced&&this.active){const target=this.target;this.stop();this.node.scrollTop=this.clamp(target);}
  }
  follow(y:number,force=false){
    if(!Number.isFinite(y)||this.node.clientHeight<=0)return;
    const relative=y-this.node.scrollTop;
    if(!force&&!this.active&&relative>=this.node.clientHeight*.35&&relative<=this.node.clientHeight*.7)return;
    const next=this.clamp(y-this.node.clientHeight*.55);
    if((next-this.node.scrollTop)*this.velocity<0)this.velocity=0;
    this.target=next;
    if(this.reduced){this.stop();this.node.scrollTop=next;return;}
    if(Math.abs(next-this.node.scrollTop)<1&&!this.active)return;
    if(!this.active){this.time=null;this.onActive(true);this.frame=this.clock.request(this.tick);}
  }
  private tick=(time:number)=>{
    // Discard background time instead of turning a suspended tab into a jump.
    const dt=this.time===null?0:Math.min(.032,Math.max(0,(time-this.time)/1000));
    this.time=time;this.target=this.clamp(this.target);
    const position=this.clamp(this.position??this.node.scrollTop),distance=position-this.target,omega=14;
    const decay=Math.exp(-omega*dt),c=this.velocity+omega*distance;
    let next=this.target+(distance+c*dt)*decay;
    let velocity=(this.velocity-omega*c*dt)*decay;
    const limit=900*dt;
    if(Math.abs(next-position)>limit){next=position+Math.sign(next-position)*limit;velocity=Math.sign(next-position)*900;}
    if((this.target-position)*(this.target-next)<0){next=this.target;velocity=0;}
    this.position=this.clamp(next);this.node.scrollTop=this.position;this.velocity=velocity;
    if(Math.abs(this.target-this.position)<1&&Math.abs(velocity)<8){
      this.node.scrollTop=this.target;this.frame=null;this.time=null;this.velocity=0;this.position=null;this.onActive(false);return;
    }
    this.frame=this.clock.request(this.tick);
  };
}
