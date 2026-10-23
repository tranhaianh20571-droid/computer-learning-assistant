import {lazy,Suspense} from "react";
import type {Graphic} from "../lib/graphics";
import FunctionPlot from "./FunctionPlot";
import CircuitDiagram from "./CircuitDiagram";
import "./professionalFigure.css";

const SurfacePlot=lazy(()=>import("./SurfacePlot"));
export default function ProfessionalFigure({graphic,ink=false,onInteract}:{graphic:Graphic;ink?:boolean;onInteract?:()=>void}){
  return <div className="professional-figure" data-ink={ink} onPointerDownCapture={onInteract} onWheelCapture={onInteract}>
    {graphic.kind==="plot"?<FunctionPlot graphic={graphic} ink={ink}/>:
      graphic.kind==="circuit"?<CircuitDiagram value={graphic}/>:
      <Suspense fallback={<p role="status">曲面加载中…</p>}><SurfacePlot graphic={graphic}/></Suspense>}
  </div>;
}
