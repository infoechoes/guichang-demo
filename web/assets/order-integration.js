const text = (v) => String(v ?? "");
async function api(path, data, extra = {}) {
  const r = await fetch(path, { method: data === void 0 ? "GET" : "POST", headers: { "Content-Type": "application/json", ...extra }, body: data === void 0 ? void 0 : JSON.stringify(data) });
  const j = await r.json();
  if (!r.ok || j.error) throw Error(j.error || "\u5904\u7406\u5931\u8D25");
  return j;
}
const pricing = (action, data = {}) => api("/api/workflow/order-pricing/" + action, data);
function OrderTools({ React, batch, draft, disabled, onSave, onApply }) {
  const [busy, setBusy] = React.useState(false), [message, setMessage] = React.useState(""), [session, setSession] = React.useState(null), [projects, setProjects] = React.useState({}), [projectId, setProjectId] = React.useState(""), [date, setDate] = React.useState(""), [results, setResults] = React.useState({}), [line, setLine] = React.useState(""), [factor, setFactor] = React.useState("1"), [mappings, setMappings] = React.useState(null);
  const current = React.useRef(draft);
  current.current = draft;
  const row = draft.rows.find((r) => r.sourceLineId === line) || draft.rows[0];
  const expected = JSON.stringify(draft);
  const [rule, setRule] = React.useState({ name: "", purpose: "sales", basis: "average_price", adjustment_kind: "rate", adjustment: "0", fee: "0", fee_order: "before", rounding: "ROUND", fixed_price: "0" });
  async function run(fn) {
    if (busy || disabled) return;
    setBusy(true);
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  function apply(next, key = expected) {
    if (JSON.stringify(current.current) !== key) throw Error("\u8BA2\u5355\u5DF2\u4FEE\u6539\uFF0C\u65E7\u7ED3\u679C\u672A\u5E94\u7528\uFF1B\u8BF7\u4FDD\u5B58\u540E\u91CD\u8BD5");
    onApply(next, key);
    setSession(null);
    setResults({});
    setMappings(null);
  }
  async function saved() {
    if (!await onSave()) throw Error("\u4FDD\u5B58\u671F\u95F4\u6709\u65B0\u4FEE\u6539\uFF0C\u8BF7\u91CD\u65B0\u4FDD\u5B58");
    return api("/api/workflow/batches/" + batch.batchId);
  }
  async function start() {
    const record = await saved();
    const s = await pricing("start", { batchId: batch.batchId, draftHash: record.draftHash, projectId, date });
    setSession({ ...s, expected });
    setResults({});
    setMessage(`\u672C\u5355 ${s.rowCount} \u884C\u5DF2\u63A5\u5165\u67E5\u4EF7\uFF0C\u65E0\u9700\u91CD\u65B0\u4E0A\u4F20\u3002`);
  }
  async function query() {
    const result2 = await pricing("query", { id: session.id, sourceLineId: row.sourceLineId });
    setResults((v) => ({ ...v, [row.sourceLineId]: result2 }));
  }
  async function adoptSource() {
    if (!row || !text(row.sourcePrice).trim()) throw Error("\u672C\u884C\u6CA1\u6709\u539F\u5355\u4EF7\u683C");
    if (!row.unit || row.unit !== row.sourceDemandUnit) throw Error("\u539F\u5355\u4E0E\u5F53\u524D\u5355\u4F4D\u4E0D\u540C\uFF0C\u8BF7\u5148\u6838\u5BF9\u6362\u7B97");
    const rows = draft.rows.map((r) => r === row ? { ...r, price: text(r.sourcePrice), priceSource: "\u4EBA\u5DE5\u6838\u5BF9\u539F\u5355\u4EF7\u683C", priceNeedsReview: false } : r);
    apply({ ...draft, header: { ...draft.header, reviewed: false }, rows });
    setMessage("\u5DF2\u5E26\u5165\u539F\u5355\u4EF7\u683C\uFF1B\u8BF7\u6838\u5BF9\u672C\u6B21\u5546\u54C1\u540E\u4FDD\u5B58\u5E95\u7A3F\u3002");
  }
  async function quote() {
    await saved();
    const s = await api("/api/session");
    const headers = { "X-Quote-Owner": s.user.id, "X-CSRF-Token": s.csrf };
    const q = await api("/api/quote/workflow", void 0, headers);
    const rows = batch.snapshot.rows.map((r, i) => ({ sourceRowId: text(r.id || i + 1), originalName: text(r.name), spec: text(r.spec), quantity: r.quantity, unit: text(r.unit), originalRow: r }));
    const payload = { sourceDocumentId: batch.parentBatchId || batch.batchId, sourceLabel: batch.customerOriginal?.filename || draft.rows[0]?.sourceFile || "\u672C\u5355\u539F\u59CB\u9700\u6C42", rows };
    const frame = document.querySelector("#gc-quote-panel iframe");
    const importer = frame?.contentWindow?.GCQuoteImportRows;
    const result2 = importer ? await importer(payload) : await api("/api/quote/workflow/import", { revision: q.revision, ...payload }, headers);
    setMessage(`\u672C\u5355 ${rows.length} \u4E2A\u539F\u884C\u5DF2\u63A5\u5230\u4E09\u65B9\u62A5\u4EF7\uFF0C\u62A5\u4EF7\u5DE5\u4F5C\u533A\u5171 ${result2.rows.length} \u884C\uFF1B\u540C\u4E00\u539F\u884C\u518D\u6B21\u53D1\u9001\u4E0D\u4F1A\u65B0\u589E\u3002`);
  }
  const result = results[row?.sourceLineId];
  const ready = Object.keys(results).filter((id) => results[id].confirmed);
  return /* @__PURE__ */ React.createElement("section", { className: "flow-card", "aria-label": "\u672C\u5355\u8FDE\u7EED\u5904\u7406" }, /* @__PURE__ */ React.createElement("h2", null, "\u672C\u5355\u67E5\u4EF7\u3001\u5E95\u7A3F\u4E0E\u62A5\u4EF7\u8854\u63A5"), /* @__PURE__ */ React.createElement("p", null, "\u6CBF\u7528\u5F53\u524D ", draft.rows.length, " \u4E2A\u539F\u884C\u3002\u53C2\u8003\u4EF7\u4FDD\u7559\u65E5\u671F\u3001\u89C4\u5219\u548C\u6765\u6E90\uFF0C\u6700\u7EC8\u6210\u4EA4\u4ECD\u9700\u6838\u5BF9\u3002"), /* @__PURE__ */ React.createElement("div", { className: "flow-actions" }, /* @__PURE__ */ React.createElement("button", { disabled: disabled || busy, onClick: () => run(async () => {
    await saved();
    const a = document.createElement("a");
    a.href = `/api/workflow/batches/${batch.batchId}/files/draft.xlsx`;
    a.download = "";
    a.click();
    setMessage("\u5F53\u524D\u5E95\u7A3F\u5DF2\u751F\u6210\uFF0C\u5305\u542B\u539F\u884C\u3001\u4EF7\u683C\u548C\u6620\u5C04\u3002");
  }) }, "\u4FDD\u5B58\u5E76\u4E0B\u8F7D\u5F53\u524D\u5E95\u7A3F"), /* @__PURE__ */ React.createElement("button", { disabled: disabled || busy, onClick: () => run(quote) }, "\u672C\u5355\u539F\u884C\u9001\u4E09\u65B9\u62A5\u4EF7"), /* @__PURE__ */ React.createElement("button", { disabled: busy, onClick: () => document.getElementById("gc-quote-tab")?.click() }, "\u67E5\u770B\u4E09\u65B9\u62A5\u4EF7")), /* @__PURE__ */ React.createElement("label", null, "\u5F53\u524D\u5546\u54C1", /* @__PURE__ */ React.createElement("select", { "aria-label": "\u8854\u63A5\u5546\u54C1\u884C", value: row?.sourceLineId || "", onChange: (e) => {
    setLine(e.target.value);
    setMappings(null);
  } }, draft.rows.map((r, i) => /* @__PURE__ */ React.createElement("option", { key: r.sourceLineId, value: r.sourceLineId }, i + 1, ". ", r.name, " \xB7 ", r.unit)))), /* @__PURE__ */ React.createElement("div", { className: "flow-actions" }, /* @__PURE__ */ React.createElement("button", { disabled: disabled || busy || !text(row?.sourcePrice).trim(), onClick: () => run(adoptSource) }, "\u6838\u5BF9\u540E\u91C7\u7528\u539F\u5355\u4EF7\u683C ", row?.sourcePrice), /* @__PURE__ */ React.createElement("button", { disabled: disabled || busy, onClick: () => run(async () => {
    const r = await api("/api/workflow/mappings/lookup", { header: draft.header, row });
    setMappings({ ...r, key: expected });
  }) }, "\u67E5\u627E\u5DF2\u786E\u8BA4\u5E95\u7A3F\u6620\u5C04")), mappings?.key === expected && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, mappings.message), !mappings.candidates.length && /* @__PURE__ */ React.createElement("p", null, "\u6CA1\u6709\u7B26\u5408\u6761\u4EF6\u7684\u5DF2\u786E\u8BA4\u6620\u5C04\u3002"), mappings.candidates.map((c, i) => /* @__PURE__ */ React.createElement("p", { key: i }, c.row.name, " \xB7 ", c.row.code, " \xB7 ", c.row.unit, " ", /* @__PURE__ */ React.createElement("a", { href: c.downloadUrl }, "\u539F\u786E\u8BA4\u5E95\u7A3F"), " ", /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled, onClick: () => run(async () => {
    const r = await api("/api/workflow/mappings/apply", { header: draft.header, row, mappingBatchId: c.batchId, mappingLineId: c.sourceLineId });
    apply({ ...draft, header: { ...draft.header, reviewed: false }, rows: draft.rows.map((x) => x === row ? r.row : x) });
  }) }, "\u6838\u5BF9\u540E\u590D\u7528")))), /* @__PURE__ */ React.createElement("details", null, /* @__PURE__ */ React.createElement("summary", null, "\u672C\u5355\u67E5\u8BE2\u53C2\u8003\u4EF7\u683C"), /* @__PURE__ */ React.createElement("div", { className: "flow-actions" }, /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled, onClick: () => run(async () => setProjects(await pricing("projects"))) }, "\u8BFB\u53D6\u5DF2\u4FDD\u5B58\u9879\u76EE\u89C4\u5219"), /* @__PURE__ */ React.createElement("select", { "aria-label": "\u672C\u5355\u4EF7\u683C\u89C4\u5219", value: projectId, onChange: (e) => {
    setProjectId(e.target.value);
    setSession(null);
    setResults({});
  } }, /* @__PURE__ */ React.createElement("option", { value: "" }, "\u9009\u62E9\u5DF2\u786E\u8BA4\u89C4\u5219"), Object.values(projects).map((r) => /* @__PURE__ */ React.createElement("option", { key: r.id, value: r.id }, r.name, " \xB7 v", r.version))), /* @__PURE__ */ React.createElement("input", { "aria-label": "\u884C\u60C5\u65E5\u671F", type: "date", value: date, onChange: (e) => {
    setDate(e.target.value);
    setSession(null);
    setResults({});
  } }), /* @__PURE__ */ React.createElement("button", { disabled: disabled || busy, onClick: () => run(start) }, "\u4FDD\u5B58\u672C\u5355\u5E76\u5F00\u59CB\u67E5\u4EF7")), /* @__PURE__ */ React.createElement("details", null, /* @__PURE__ */ React.createElement("summary", null, "\u7EF4\u62A4\u9879\u76EE\u89C4\u5219"), /* @__PURE__ */ React.createElement("p", null, "\u8BF7\u6309\u5B9E\u9645\u9879\u76EE\u7EA6\u5B9A\u586B\u5199\uFF1B\u4E0B\u6D6E5%\u586B -0.05\uFF0C\u7CFB\u657095%\u586B 0.95\u3002"), /* @__PURE__ */ React.createElement("div", { className: "flow-grid" }, [["name", "\u9879\u76EE\u540D\u79F0"], ["fee", "\u52A0\u5DE5\u8D39"], ["adjustment", "\u6D6E\u52A8\u503C\u6216\u7CFB\u6570"], ["fixed_price", "\u56FA\u5B9A\u4EF7"]].map(([key, label]) => /* @__PURE__ */ React.createElement("label", { key }, label, /* @__PURE__ */ React.createElement("input", { "aria-label": label, value: rule[key], onChange: (e) => setRule({ ...rule, [key]: e.target.value }) }))), [["purpose", "\u7528\u9014", [["sales", "\u9500\u552E\u53C2\u8003"], ["purchase", "\u91C7\u8D2D\u53C2\u8003"]]], ["basis", "\u57FA\u51C6", [["average_price", "\u5E73\u5747\u4EF7"], ["low_price", "\u6700\u4F4E\u4EF7"], ["high_price", "\u6700\u9AD8\u4EF7"], ["fixed", "\u56FA\u5B9A\u4EF7"]]], ["adjustment_kind", "\u6D6E\u52A8\u53E3\u5F84", [["rate", "\u589E\u51CF\u7387"], ["coefficient", "\u4E58\u6570"]]], ["fee_order", "\u52A0\u5DE5\u8D39\u987A\u5E8F", [["before", "\u5148\u52A0\u8D39\u7528\u518D\u6D6E\u52A8"], ["after", "\u5148\u6D6E\u52A8\u518D\u52A0\u8D39\u7528"]]], ["rounding", "\u53D6\u6574", [["ROUND", "\u56DB\u820D\u4E94\u5165\u4E24\u4F4D"], ["ROUNDDOWN", "\u622A\u53D6\u4E24\u4F4D"], ["none", "\u4E0D\u53D6\u6574"]]]].map(([key, label, choices]) => /* @__PURE__ */ React.createElement("label", { key }, label, /* @__PURE__ */ React.createElement("select", { "aria-label": label, value: rule[key], onChange: (e) => setRule({ ...rule, [key]: e.target.value }) }, choices.map(([id, name]) => /* @__PURE__ */ React.createElement("option", { key: id, value: id }, name)))))), /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled, onClick: () => run(async () => {
    const r = await pricing("projects", { project: rule });
    setProjects(await pricing("projects"));
    setProjectId(r.id);
    setSession(null);
    setMessage("\u5DF2\u4FDD\u5B58\u9879\u76EE\u89C4\u5219\uFF0C\u8BF7\u6838\u5B9E\u9002\u7528\u65E5\u671F\u548C\u7528\u9014\u3002");
  }) }, "\u4FDD\u5B58\u89C4\u5219")), session && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, "\u672C\u6B21\u63A5\u5165 ", session.rowCount, " \u884C\uFF0C\u5DF2\u786E\u8BA4 ", ready.length, " \u884C\u3002"), /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled || session.expected !== expected, onClick: () => run(query) }, "\u67E5\u8BE2\u5F53\u524D\u5546\u54C1"), session.expected !== expected && /* @__PURE__ */ React.createElement("p", null, "\u8BA2\u5355\u5DF2\u53D8\u5316\uFF0C\u8BF7\u4FDD\u5B58\u5E76\u91CD\u65B0\u5F00\u59CB\u67E5\u4EF7\u3002"), result && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, result.status), /* @__PURE__ */ React.createElement("label", null, "1 \u5F53\u524D\u5355\u4F4D\u5BF9\u5E94\u591A\u5C11\u884C\u60C5\u5355\u4F4D", /* @__PURE__ */ React.createElement("input", { "aria-label": "\u884C\u60C5\u5355\u4F4D\u6362\u7B97\u6BD4\u4F8B", value: factor, onChange: (e) => setFactor(e.target.value) })), (result.items || []).map((item, i) => /* @__PURE__ */ React.createElement("p", { key: i }, item.name, " \xB7 ", item.spec, " \xB7 ", item.origin, " \xB7 ", item.unit, " \xB7 ", item.average_price, " \xB7 ", item.published_at, " ", /* @__PURE__ */ React.createElement("a", { href: item.reference_url, target: "_blank", rel: "noreferrer" }, "\u539F\u6765\u6E90"), " ", /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled, onClick: () => run(async () => {
    const r = await pricing("confirm", { id: session.id, sourceLineId: row.sourceLineId, index: i, factor });
    setResults((v) => ({ ...v, [row.sourceLineId]: r }));
  }) }, "\u786E\u8BA4\u6B64\u5019\u9009\u4E0E\u6362\u7B97")))), /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled || session.expected !== expected, onClick: () => run(async () => {
    const r = await pricing("apply", { id: session.id, sourceLineIds: ready });
    apply(r.draft, session.expected);
    setMessage(`\u5DF2\u5E26\u56DE ${r.appliedCount}/${r.totalCount} \u884C\u53C2\u8003\u4EF7\uFF0C\u5176\u4F59\u539F\u884C\u4E0E\u539F\u4EF7\u5B8C\u6574\u4FDD\u7559\uFF0C\u5F85\u6838 ${r.counts.pending} \u884C\uFF1B\u8BF7\u590D\u6838\u5E76\u4FDD\u5B58\u5E95\u7A3F\u3002`);
  }) }, "\u6574\u5355\u5E26\u56DE\u53C2\u8003\u4EF7\u4E0E\u5F85\u6838\u72B6\u6001"))), message && /* @__PURE__ */ React.createElement("p", { role: "status" }, message));
}
function InvoiceMappingTools({ React, draft, onChange, disabled }) {
  const [result, setResult] = React.useState(null), [message, setMessage] = React.useState(""), [busy, setBusy] = React.useState(false);
  const latest = React.useRef(draft);
  latest.current = draft;
  const key = JSON.stringify(draft);
  const header = { ...draft.header, knowledgeBaseId: draft.header.knowledgeBaseId || "default-catalog", agentId: draft.header.customerId, invoiceAgentId: draft.header.customerId };
  async function lookup() {
    setBusy(true);
    setMessage("");
    try {
      const found = [];
      for (let i = 0; i < draft.rows.length; i++) {
        const row = draft.rows[i];
        if (row.code) continue;
        const r = await api("/api/workflow/mappings/lookup", { header, row: { originalCode: row.sourceCode, sourceName: row.name, sourceSpec: row.spec, sourceDemandUnit: row.unit } });
        found.push({ index: i, ...r });
      }
      if (JSON.stringify(latest.current) === key) setResult({ key, found });
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  function apply(index, c) {
    if (JSON.stringify(latest.current) !== key) {
      setMessage("\u5F00\u7968\u6E05\u5355\u5DF2\u53D8\u5316\uFF0C\u8BF7\u91CD\u65B0\u67E5\u8BE2");
      return;
    }
    if (c.row.unit !== draft.rows[index].unit) {
      setMessage("\u5E95\u7A3F\u7528\u53CB\u5355\u4F4D\u4E0E\u672C\u6B21\u5F00\u7968\u5355\u4F4D\u4E0D\u540C\uFF0C\u8BF7\u5148\u786E\u8BA4\u6362\u7B97\uFF1B\u672A\u6539\u6570\u91CF\u6216\u4EF7\u683C");
      return;
    }
    onChange({ ...draft, rows: draft.rows.map((r, i) => i === index ? { ...r, code: c.row.code, mappingReference: { batchId: c.batchId, sourceLineId: c.sourceLineId, planId: c.planId } } : r) });
    setResult(null);
    setMessage("\u5DF2\u590D\u7528\u7528\u53CB\u7F16\u7801\uFF0C\u5F00\u7968\u6570\u91CF\u3001\u4EF7\u683C\u548C\u7A0E\u7387\u4FDD\u7559\uFF1B\u4E0B\u63A8\u524D\u4ECD\u6309\u539F\u6D41\u7A0B\u6821\u9A8C\u3002");
  }
  return /* @__PURE__ */ React.createElement("div", { className: "invoice-actions" }, /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled, onClick: lookup }, "\u4ECE\u5DF2\u786E\u8BA4\u5F55\u5355\u5E95\u7A3F\u5339\u914D\u5F00\u7968\u7F16\u7801"), message && /* @__PURE__ */ React.createElement("p", { role: "status" }, message), result?.key === key && result.found.map((r) => /* @__PURE__ */ React.createElement("div", { key: r.index }, "\u7B2C", r.index + 1, "\u884C\uFF1A", !r.candidates.length ? "\u6CA1\u6709\u7CBE\u786E\u5339\u914D" : r.candidates.map((c, i) => /* @__PURE__ */ React.createElement("span", { key: i }, c.row.code, " \xB7 ", c.row.name, " ", /* @__PURE__ */ React.createElement("button", { disabled: busy || disabled, onClick: () => apply(r.index, c) }, "\u6838\u5BF9\u540E\u91C7\u7528\u6B64\u7F16\u7801"))))));
}
export {
  InvoiceMappingTools,
  OrderTools
};
