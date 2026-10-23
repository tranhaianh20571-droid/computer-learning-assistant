import {useEffect,useRef,useState} from "react";
import * as THREE from "three";
import {OrbitControls} from "three/addons/controls/OrbitControls.js";
import {RotateCcw,ZoomIn,ZoomOut} from "lucide-react";
import {expression} from "../lib/graphics";
import type {Surface} from "../lib/graphics";

export default function SurfacePlot({graphic}:{graphic:Surface}){
  const host=useRef<HTMLDivElement>(null),api=useRef<{reset:()=>void;zoom:(n:number)=>void;sections:(x:boolean,y:boolean)=>void}|null>(null);
  const [error,setError]=useState(""),[sx,setX]=useState(true),[sy,setY]=useState(true),[retry,setRetry]=useState(0);
  useEffect(()=>{
    if(!host.current)return;
    let renderer:THREE.WebGLRenderer|undefined,controls:OrbitControls|undefined,observer:ResizeObserver|undefined;
    const scene=new THREE.Scene(),node=host.current;
    const dispose=()=>{observer?.disconnect();controls?.dispose();scene.traverse((o:any)=>{o.geometry?.dispose();if(Array.isArray(o.material))o.material.forEach((m:any)=>m.dispose());else o.material?.dispose();});renderer?.dispose();renderer?.domElement.remove();api.current=null;};
    try{
      const fn=expression(graphic.expression,["x","y"]),{x,y}=graphic.bounds,N=48;
      const samples:number[]=[],positions:number[]=[],indices:number[]=[],valid:boolean[]=[];
      for(let i=0;i<=N;i++)for(let j=0;j<=N;j++)samples.push(fn({x:x[0]+(x[1]-x[0])*i/N,y:y[0]+(y[1]-y[0])*j/N}));
      const finite=samples.filter(Number.isFinite);if(!finite.length)throw Error("empty surface");
      const lo=Math.min(...finite),hi=Math.max(...finite),mid=(lo+hi)/2,scale=Math.max(hi-lo,1);
      for(let i=0;i<=N;i++)for(let j=0;j<=N;j++){
        const z=samples[i*(N+1)+j];valid.push(Number.isFinite(z));
        positions.push(-2+4*i/N,Number.isFinite(z)?(z-mid)/scale*3:0,-2+4*j/N);
      }
      for(let i=0;i<N;i++)for(let j=0;j<N;j++){
        const a=i*(N+1)+j,b=a+N+1;
        if([a,a+1,b,b+1].every(k=>valid[k])&&Math.max(...[a,a+1,b,b+1].map(k=>samples[k]))-Math.min(...[a,a+1,b,b+1].map(k=>samples[k]))<scale*.6)
          indices.push(a,b,a+1,b,b+1,a+1);
      }
      if(!indices.length)throw Error("empty surface");
      renderer=new THREE.WebGLRenderer({alpha:true,antialias:true,preserveDrawingBuffer:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));node.prepend(renderer.domElement);
      const camera=new THREE.PerspectiveCamera(40,1,.1,100);camera.position.set(5,4.2,5.4);
      controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=false;controls.enablePan=false;controls.minDistance=4;controls.maxDistance=15;
      controls.saveState();
      scene.add(new THREE.HemisphereLight(0xffffff,0x54715c,2.8));
      const light=new THREE.DirectionalLight(0xffffff,2);light.position.set(3,8,6);scene.add(light);
      const geometry=new THREE.BufferGeometry();geometry.setAttribute("position",new THREE.Float32BufferAttribute(positions,3));geometry.setIndex(indices);geometry.computeVertexNormals();
      scene.add(new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:0x9fc8b2,roughness:.72,side:THREE.DoubleSide})));
      scene.add(new THREE.LineSegments(new THREE.WireframeGeometry(geometry),new THREE.LineBasicMaterial({color:0x537e67,transparent:true,opacity:.16})));
      const grid=new THREE.GridHelper(5,10,0x7e9c88,0x6a8775);grid.position.y=-1.7;scene.add(grid);
      const makeSection=(which:"x"|"y",color:number)=>{
        let points:THREE.Vector3[]=[];
        const group=new THREE.Group();
        const flush=()=>{if(points.length>1)group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),new THREE.LineBasicMaterial({color})));points=[];};
        const range=which==="x"?x:y;
        const other=which==="x"?y:x;
        if(!(other[0]<=0&&other[1]>=0))return group;
        for(let i=0;i<=120;i++){
          const t=range[0]+(range[1]-range[0])*i/120,z=fn(which==="x"?{x:t,y:0}:{x:0,y:t});
          if(!Number.isFinite(z)){flush();continue;}
          if(points.length&&Math.abs((z-mid)/scale*3-points.at(-1)!.y)>1.8)flush();
          points.push(new THREE.Vector3(which==="x"?-2+4*i/120:-2+(0-x[0])/(x[1]-x[0])*4,(z-mid)/scale*3+.015,which==="y"?-2+4*i/120:-2+(0-y[0])/(y[1]-y[0])*4));
        }
        flush();return group;
      };
      const cx=makeSection("x",0xffdc8c),cy=makeSection("y",0xeeb7ac);scene.add(cx,cy);
      const axes=new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(-2.4,-1.7,0),new THREE.Vector3(2.4,-1.7,0),
        new THREE.Vector3(0,-1.7,-2.4),new THREE.Vector3(0,-1.7,2.4),
        new THREE.Vector3(0,-1.7,0),new THREE.Vector3(0,2,0)]),new THREE.LineBasicMaterial({color:0xb4c9b9}));scene.add(axes);
      const render=()=>{
        if(!node.clientWidth||!node.clientHeight||!renderer)return;
        renderer.setSize(node.clientWidth,node.clientHeight,false);camera.aspect=node.clientWidth/node.clientHeight;camera.updateProjectionMatrix();renderer.render(scene,camera);
        for(const [axis,p]of Object.entries({x:[2.5,-1.7,0],y:[0,-1.7,2.5],z:[0,2.1,0]})){
          const pos=new THREE.Vector3(...p).project(camera),label=node.querySelector<HTMLElement>(`[data-axis=${axis}]`);
          if(label){label.style.left=`${Math.max(4,Math.min(node.clientWidth-20,(pos.x+1)*node.clientWidth/2))}px`;label.style.top=`${Math.max(2,Math.min(node.clientHeight-22,(1-pos.y)*node.clientHeight/2))}px`;}
        }
      };
      controls.addEventListener("change",render);
      renderer.domElement.addEventListener("webglcontextlost",e=>{e.preventDefault();setError("三维画面暂不可用，可重新加载。");});
      api.current={reset:()=>{controls?.reset();render();},zoom:n=>{camera.position.multiplyScalar(n);controls?.update();render();},sections:(a,b)=>{cx.visible=a;cy.visible=b;render();}};
      observer=new ResizeObserver(render);observer.observe(node);render();setError("");
      return dispose;
    }catch{dispose();setError("曲面无法显示，请检查表达式或重新加载。");}
  },[graphic.expression,JSON.stringify(graphic.bounds),retry]);
  useEffect(()=>api.current?.sections(sx,sy),[sx,sy,graphic.expression,retry]);
  return <div className="surface-plot">
    <div ref={host} className="surface-plot-host" role="img" aria-label={`曲面 z = ${graphic.expression}`}>
      {["x","y","z"].map(a=><span key={a} className="surface-axis" data-axis={a}>{a}</span>)}
    </div>
    {error?<p className="figure-error" role="alert">{error}<button type="button" onClick={()=>setRetry(n=>n+1)}>重新加载</button></p>:
      <div className="figure-tools">
        <label><input type="checkbox" checked={sx} onChange={e=>setX(e.target.checked)}/>y = 0 截面</label>
        <label><input type="checkbox" checked={sy} onChange={e=>setY(e.target.checked)}/>x = 0 截面</label>
        <button type="button" title="复位视角" aria-label="复位视角" onClick={()=>api.current?.reset()}><RotateCcw size={16}/></button>
        <button type="button" title="放大曲面" aria-label="放大曲面" onClick={()=>api.current?.zoom(.85)}><ZoomIn size={16}/></button>
        <button type="button" title="缩小曲面" aria-label="缩小曲面" onClick={()=>api.current?.zoom(1.15)}><ZoomOut size={16}/></button>
      </div>}
    <div className="surface-range">x ∈ [{graphic.bounds.x.join(", ")}]　y ∈ [{graphic.bounds.y.join(", ")}]　z = {graphic.expression}</div>
  </div>;
}
