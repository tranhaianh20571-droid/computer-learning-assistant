import QuestionText from "./QuestionText";
import ProfessionalFigure from "./ProfessionalFigure";
import {useState} from "react";
import {RotateCcw} from "lucide-react";

export default function AssessmentFigure({ figure }: { figure: any }) {
  const [failedUrl,setFailedUrl]=useState(""),[attempt,setAttempt]=useState(0);
  const source = figure?.source;
  const url = figure?.kind === "material_figure" && source
    ? typeof source.asset_index==="number"
      ? `/api/source-materials/${encodeURIComponent(source.material_id)}/assets/${source.asset_index}`
      : source.figure_id
      ? `/api/materials/${encodeURIComponent(source.material_id)}/figures/${encodeURIComponent(source.figure_id)}/content`
      : `/api/attachments/${encodeURIComponent(source.material_id)}/images/${source.page_no}/${source.block_no}${
        source.crop_sha256?`?crop_sha256=${encodeURIComponent(source.crop_sha256)}`:""}`
    : null;
  if (!figure || figure.kind === "none") return null;
  const chart = figure.chart_spec;
  const colors = ["#2959b6", "#b34837", "#327954", "#8251a0"];
  let drawing = null;
  if (chart?.series?.length && chart.labels?.length >= 2) {
    const values = chart.series.flatMap((s: any) => s.values) as number[];
    const low = Math.min(0, ...values), high = Math.max(0, ...values);
    const y = (value: number) => 215 - (value - low) / (high - low || 1) * 180;
    const xs: number[] = chart.chart_type === "scatter" ? chart.x_values : chart.labels.map((_: string, i: number) => i);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const x = (i: number) => 65 + (xs[i] - minX) / (maxX - minX || 1) * 420;
    drawing = <div className="assessment-chart">
      <div className="chart-axis-label">{chart.y_label}</div>
      <svg viewBox="0 0 560 280" role="img" aria-label={`${chart.y_label} / ${chart.x_label}`}>
        <line x1="55" x2="505" y1={y(0)} y2={y(0)} stroke="currentColor" />
        <line x1="55" x2="55" y1="30" y2="220" stroke="currentColor" />
        {[low, high].map((v, i) => <text key={i} x="0" y={y(v)} fontSize="20">{v.toPrecision(3)}</text>)}
        {chart.labels.map((label: string, i: number) => i % Math.max(1, Math.ceil(chart.labels.length / 6)) === 0
          && <text key={i} x={x(i)} y="250" textAnchor="middle" fontSize="20">
            <title>{label}</title>{label.length > 7 ? label.slice(0, 6) + "…" : label}</text>)}
        {chart.series.map((s: any, seriesIndex: number) => <g key={seriesIndex} fill={colors[seriesIndex]}>
          {chart.chart_type === "line" && <polyline fill="none" stroke={colors[seriesIndex]} strokeWidth="2"
            points={s.values.map((v: number, i: number) => `${x(i)},${y(v)}`).join(" ")} />}
          {s.values.map((value: number, i: number) => chart.chart_type === "bar"
            ? <rect key={i} x={x(i) - 12 + seriesIndex * (24 / chart.series.length)}
                y={Math.min(y(value), y(0))} width={24 / chart.series.length}
                height={Math.max(1, Math.abs(y(value) - y(0)))} />
            : <circle key={i} cx={x(i)} cy={y(value)} r="3" />)}
        </g>)}
      </svg>
      <div className="chart-axis-label chart-x-label">{chart.x_label}</div>
      <div className="chart-legend">{chart.series.map((s: any, i: number) =>
        <span key={i}><i style={{ background: colors[i] }} />{s.label}</span>)}</div>
    </div>;
  }
  return <figure className="assessment-figure">
    {url&&failedUrl!==url&&<img key={`${url}:${attempt}`} src={url} alt={figure.caption || "题目原图"}
      onError={()=>setFailedUrl(url)}/>}
    {url&&failedUrl===url&&<div role="alert">题图加载失败
      <button className="icon-btn" aria-label="重试题图" title="重试题图" onClick={()=>{setFailedUrl("");setAttempt(v=>v+1);}}><RotateCcw size={17}/></button>
    </div>}
    {drawing}
    {figure.kind==="generated_figure"&&figure.graphic&&<ProfessionalFigure graphic={figure.graphic}/>}
    {figure.kind === "data_table" && <div className="assessment-table"><table>
      <thead><tr>{figure.table.columns.map((c: string, i: number) => <th key={i}><QuestionText text={c} /></th>)}</tr></thead>
      <tbody>{figure.table.rows.map((row: string[], i: number) => <tr key={i}>
        {row.map((cell, j) => <td key={j}><QuestionText text={cell} /></td>)}</tr>)}</tbody>
    </table></div>}
    {figure.caption && <figcaption><QuestionText text={figure.caption} /></figcaption>}
  </figure>;
}
