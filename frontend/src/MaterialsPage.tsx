import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { BookOpen, FileText, FileType2, FolderOpen, Plus, Search, Trash2, Upload } from "lucide-react";

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
  const [nameQuery, setNameQuery] = useState("");
  const [kindFilter, setKindFilter] = useState("all");
  const [hits, setHits] = useState<any[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const totalBytes = useMemo(() => materials.reduce((size, item) => size + (item.size_bytes || 0), 0), [materials]);
  const visibleMaterials = useMemo(() => materials.filter(material => {
    const kindMatches = kindFilter === "all" || material.kind === kindFilter;
    const query = nameQuery.trim().toLowerCase();
    const queryMatches = !query || `${material.material_id} ${material.kind}`.toLowerCase().includes(query);
    return kindMatches && queryMatches;
  }), [materials, kindFilter, nameQuery]);
  const kinds = useMemo(() => [...new Set(materials.map(material => material.kind))].sort(), [materials]);

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
    <div className="knowledge-page">
      <header className="knowledge-heading">
        <div><span className="kicker">个人知识库</span><h1>我的资料</h1><p>整理资料，查看处理状态，并在选定资料中检索。</p></div>
        <label className="knowledge-upload">
          <input type="file" multiple accept=".pdf,.md,.markdown,.txt" disabled={uploading}
            onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
          <Upload size={16} aria-hidden="true" />{uploading ? "正在上传…" : "添加资料"}
        </label>
      </header>
      <div className="knowledge-layout">
        <aside className="knowledge-sidebar" aria-label="资料主题">
          <h2>资料主题</h2>
          <button className="knowledge-filter" aria-current={kindFilter === "all"} onClick={() => setKindFilter("all")}>
            <BookOpen aria-hidden="true" /><span>全部资料</span><b>{materials.length}</b>
          </button>
          {kinds.map(kind => <button key={kind} className="knowledge-filter" aria-current={kindFilter === kind} onClick={() => setKindFilter(kind)}>
            <FileType2 aria-hidden="true" /><span>{kind.toUpperCase()}</span><b>{materials.filter(item => item.kind === kind).length}</b>
          </button>)}
          <div className="knowledge-quota"><span>存储空间</span><strong>{totalBytes < 1024 * 1024
            ? `${Math.ceil(totalBytes / 1024)} KiB`
            : `${(totalBytes / (1024 * 1024)).toFixed(1)} MiB`}</strong></div>
        </aside>
        <section className="knowledge-main" aria-label="资料">
          <div className="knowledge-toolbar">
            <label className="knowledge-search"><Search size={17} aria-hidden="true" /><input type="search" value={nameQuery} onChange={event => setNameQuery(event.target.value)} placeholder="搜索资料 ID 或类型" aria-label="搜索资料 ID 或类型" /></label>
            <span className="knowledge-result-count">{visibleMaterials.length} 份资料</span>
          </div>
          {materials.length === 0 ? <div className="knowledge-empty"><FolderOpen size={26} aria-hidden="true" /><h2>知识库还是空的</h2><p>添加 PDF、Markdown 或 TXT 资料，开始整理个人知识库。</p>
            <label className="knowledge-upload"><input type="file" multiple accept=".pdf,.md,.markdown,.txt" disabled={uploading}
              onChange={(e) => { upload(e.target.files); e.target.value = ""; }} /><Plus size={16} />选择资料</label>
          </div> : visibleMaterials.length === 0 ? <div className="knowledge-empty"><h2>没有匹配的资料</h2><p>更换搜索内容或资料类型。</p><button className="btn" onClick={() => { setNameQuery(""); setKindFilter("all"); }}>清除筛选</button></div> :
          <div className="knowledge-materials" aria-label="我的资料">
            {visibleMaterials.map((m) => (
              <article className={`knowledge-material${selected === m.material_id ? " is-selected" : ""}`} key={m.material_id}>
                <button className="knowledge-material-select" aria-current={selected === m.material_id} onClick={() => setSelected(m.material_id)}>
                  <span className="knowledge-file-icon">{m.kind === "pdf" ? <FileText aria-hidden="true" /> : <FileType2 aria-hidden="true" />}</span>
                  <span className="knowledge-material-name"><strong>资料 {m.material_id.slice(0, 12)}</strong><small>{m.kind.toUpperCase()} · {Math.round((m.size_bytes || 0) / 1024)} KiB</small></span>
                  <MatState status={m.status} />
                  <span className="knowledge-material-date">{m.created_at ? new Date(m.created_at).toLocaleDateString() : ""}</span>
                </button>
                {selected === m.material_id && <div className="knowledge-material-actions">
                  <span>删除后将立即从所有检索范围移除。</span><button className="btn btn-sm btn-danger" onClick={() => remove(m.material_id)}><Trash2 size={14} />删除资料</button>
                </div>}
              </article>
            ))}
          </div>}
          <div className="knowledge-footer"><span>资料仅自己可见</span><span>最多支持 10 份同时上传 · 单文件不超过 20 MiB</span></div>
        </section>
        <section className="knowledge-detail" aria-label="资料处理与检索">
        {selected && (
          <>
            <div className="knowledge-detail-heading">
              <div><span className="kicker">资料详情</span><h2>资料 {selected.slice(0, 12)}</h2></div>
              <span className="knowledge-detail-kind">{materials.find(item => item.material_id === selected)?.kind.toUpperCase()}</span>
            </div>
            <div>
              <span className="kicker">逐页处理状态</span>
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
            <div className="knowledge-retrieval">
              <span className="kicker">内容检索</span>
              <label className="knowledge-search"><Search size={16} aria-hidden="true" /><input
                placeholder="查找一个概念或问题" value={query} onChange={(event) => { setQuery(event.target.value); setHits(null); }}
                onKeyDown={(e) => { if (e.key === "Enter") search(); }} aria-label="检索关键词" />
              </label>
              <button className="btn btn-ink" disabled={!query.trim()} onClick={search}>检索当前资料</button>
              {hits !== null && <ul className="regions">
                {hits.map((h) => <li key={h.chunk_id}><span className="rk">p{h.page_no}</span><span>chunk {h.chunk_id.slice(0, 12)} {h.figure_asset_id ? "· 关联图块" : ""}</span><span className="src src-book">资料</span></li>)}
                {hits.length === 0 && <li className="help">当前资料没有命中。</li>}
              </ul>}
            </div>
          </>
        )}
        {!selected && <div className="knowledge-detail-empty"><FolderOpen size={24} aria-hidden="true" /><p>选择一份资料，查看处理状态或检索内容。</p></div>}
        </section>
      </div>
    </div>
  );
}
