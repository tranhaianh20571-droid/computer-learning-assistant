import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/**
 * 资料页（切片 2 T07，设计稿 3 节）：
 * - 上传区（多选，20 MiB/10 文件边界由服务端校验并回显）；
 * - 资料列表（状态字形 .st-mat：已解析/部分可用/解析失败）+ 删除（立即脱离检索）；
 * - 逐页状态网格（页方块：success/partial/failed）+ 失败页重试；
 * - 检索面板：仅本人选定资料，命中带页码与来源标记。
 */

const MAT_STATUS: Record<string, { text: string; cls: string }> = {
  parsed: { text: "已解析", cls: "is-ok" },
  partial: { text: "部分可用", cls: "is-partial" },
  failed: { text: "解析失败", cls: "is-fail" },
  parsing: { text: "解析中", cls: "" },
  uploaded: { text: "已上传", cls: "" },
};

function MatState({ status }: { status: string }) {
  const meta = MAT_STATUS[status] || MAT_STATUS.uploaded;
  return (
    <span className={`st st-mat ${meta.cls}`}>
      <i className="g" aria-hidden="true" />
      <span className="k">资料</span>
      {meta.text}
    </span>
  );
}

export default function MaterialsPage({ onErr, onOk }: { onErr: (s: string) => void; onOk: (s: string) => void }) {
  const [materials, setMaterials] = useState<any[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [pages, setPages] = useState<any[]>([]);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<any[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [picking, setPicking] = useState(false);

  const loadMaterials = useCallback(async () => {
    try {
      const r = await api.listMaterials();
      setMaterials(r.data || []);
      if (r.data?.length && !selected) setSelected(r.data[0].material_id);
    } catch (e: any) {
      onErr(e?.message || "资料列表加载失败");
    }
  }, [onErr, selected]);

  const loadPages = useCallback(async (mid: string) => {
    if (!mid) return;
    // 逐页状态：资料详情只有聚合状态；页面状态经 page 接口按需拉取（最多 50 页）
    const out: any[] = [];
    try {
      const detail = await api.getMaterial(mid);
      const total = Math.min(detail.page_count || 0, 50);
      for (let p = 1; p <= total; p++) {
        try {
          const pg = await api.getMaterialPage(mid, p);
          out.push(pg);
        } catch { /* 单页缺失忽略 */ }
      }
      setPages(out);
    } catch { setPages([]); }
  }, []);

  useEffect(() => { loadMaterials(); }, [loadMaterials]);
  useEffect(() => { if (selected) loadPages(selected); }, [selected, loadPages]);

  const upload = useCallback(async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    try {
      const r = await api.uploadMaterials(Array.from(files));
      onOk(`已上传 ${r.data?.length ?? 0} 个文件，解析任务已排队。`);
      await loadMaterials();
    } catch (e: any) {
      onErr(e?.code ? `${e.code}` : "上传失败");
    } finally {
      setUploading(false);
    }
  }, [loadMaterials, onErr, onOk]);

  const retryPage = useCallback(async (mid: string, pageNo: number) => {
    try {
      await api.retryMaterialPage(mid, pageNo);
      onOk(`第 ${pageNo} 页已重置为待处理。`);
      await loadPages(mid);
    } catch (e: any) {
      onErr(e?.message || "重试失败");
    }
  }, [loadPages, onErr, onOk]);

  const remove = useCallback(async (mid: string) => {
    try {
      await api.deleteMaterial(mid);
      onOk("资料已删除，检索范围立即移除。");
      setSelected("");
      setPages([]);
      await loadMaterials();
    } catch (e: any) {
      onErr(e?.message || "删除失败");
    }
  }, [loadMaterials, onErr, onOk]);

  const search = useCallback(async () => {
    if (!query.trim() || !selected) return;
    try {
      const r = await api.searchMaterials({ query, material_ids: [selected] });
      setHits(r.data || []);
    } catch (e: any) {
      onErr(e?.message || "检索失败");
    }
  }, [onErr, query, selected]);

  return (
    <div className="mat-layout">
      <div className="od-stack" style={{ ["--od-gap" as any]: "16px" }}>
        <div
          className="upzone"
          role="button"
          tabIndex={0}
          aria-label="上传资料"
          onClick={() => setPicking(true)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") setPicking(true); }}
        >
          <span className="big" aria-hidden="true">⇪</span>
          <div>
            <div className="t">{uploading ? "正在上传…" : "上传 PDF / Markdown / TXT"}</div>
            <span className="help">单文件 ≤ 20 MiB · 单次 ≤ 10 个 · 扫描 PDF ≤ 1000 页</span>
          </div>
          <button className="btn btn-sm btn-pri" disabled={uploading}
            onClick={(e) => { e.stopPropagation(); setPicking(true); }}>
            选择文件
          </button>
          <input
            type="file"
            multiple
            accept=".pdf,.md,.markdown,.txt"
            style={{ display: "none" }}
            ref={(el) => {
              if (el && picking) {
                setPicking(false);
                el.click();
              }
            }}
            onChange={(e) => { upload(e.target.files); e.target.value = ""; }}
          />
        </div>

        <ul className="mat-list" aria-label="我的资料">
          {materials.map((m) => (
            <li key={m.material_id}>
              <button
                className={`mat-item${selected === m.material_id ? "" : ""}`}
                aria-current={selected === m.material_id}
                onClick={() => setSelected(m.material_id)}
              >
                <span className="n od-truncate">资料 {m.material_id.slice(0, 12)}</span>
                <span className="m">{m.kind.toUpperCase()} · {Math.round((m.size_bytes || 0) / 1024)} KiB</span>
                <MatState status={m.status} />
              </button>
              {selected === m.material_id && (
                <div className="row" style={{ padding: "0 12px 10px 14px" }}>
                  <button className="btn btn-sm btn-danger" onClick={() => remove(m.material_id)}>删除</button>
                  <span className="help">删除后立即从所有检索范围移除。</span>
                </div>
              )}
            </li>
          ))}
          {materials.length === 0 && <li className="help" style={{ padding: 12 }}>还没有资料。上传后自动逐页解析。</li>}
        </ul>
      </div>

      <div className="od-stack" style={{ ["--od-gap" as any]: "16px", minWidth: 0 }}>
        {selected && (
          <>
            <div>
              <span className="kicker">逐页状态 · {selected.slice(0, 12)}</span>
              <div className="pages" role="group" aria-label="逐页处理结果">
                {pages.map((p) => (
                  <button
                    key={p.page_id}
                    className={`pg ${p.status === "success" ? "" : p.status}`}
                    aria-label={`第 ${p.page_no} 页 ${p.status}`}
                    title={p.failure_reason ? `第 ${p.page_no} 页失败：${p.failure_reason}` : `第 ${p.page_no} 页 ${p.status}`}
                    onClick={() => p.status === "failed" && retryPage(selected, p.page_no)}
                  />
                ))}
                {pages.length === 0 && <span className="help">解析进行中或暂无页面。</span>}
              </div>
              <div className="pg-legend">
                <span><i style={{ background: "var(--ok)" }} />成功</span>
                <span><i className="pg partial" style={{ width: 10, height: 13 }} />部分</span>
                <span><i style={{ background: "var(--err)" }} />失败（点击可重试）</span>
              </div>
            </div>

            <div className="pgdetail">
              <span className="kicker">资料检索 · 仅选定资料</span>
              <div className="row">
                <input
                  className="inp"
                  style={{ maxWidth: 320 }}
                  placeholder="检索关键词（中文/标识符/版本号）"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") search(); }}
                  aria-label="检索关键词"
                />
                <button className="btn btn-pri btn-sm" onClick={search}>检索</button>
              </div>
              {hits !== null && (
                <ul className="regions">
                  {hits.map((h) => (
                    <li key={h.chunk_id}>
                      <span className="rk">p{h.page_no}</span>
                      <span>chunk {h.chunk_id.slice(0, 12)} {h.figure_asset_id ? "· 关联图块" : ""}</span>
                      <span className="src src-book">资料</span>
                    </li>
                  ))}
                  {hits.length === 0 && <li className="help">无命中。</li>}
                </ul>
              )}
            </div>
          </>
        )}
        {!selected && <p className="muted">选择或上传一份资料后，这里显示逐页状态与检索面板。</p>}
      </div>
    </div>
  );
}
