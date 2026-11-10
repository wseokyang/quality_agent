/* Same-origin review UI. Auth stays in this closure; no browser storage or URL bearer. */
"use strict";
(() => {
  const uiPath = window.location.pathname.lastIndexOf("/ui/");
  const API = `${uiPath >= 0 ? window.location.pathname.slice(0, uiPath) : ""}/quality/v1`;
  const $ = (id) => document.getElementById(id);
  const state = { token: null, expiresAt: 0, context: {}, cases: [], profiles: [], current: null, tab: "evidence", assets: [], poll: null, pending: false, generation: 0, keys: new Map() };
  const labels = { active: "진행 중", completed: "완료", archived: "보관", unreviewed: "검토 전", adopted: "채택", needs_review: "재검토", excluded: "제외", accepted: "확정", superseded: "재확인 필요", queued: "대기", running: "진행 중", succeeded: "완료", failed: "실패", interrupted: "중단", unknown: "확인 필요", partial: "일부 결과", draft: "초안", personal: "개인 저장", submitted: "공유 검토 중", published: "팀 공유", expired: "만료", deprecated: "사용 중지", user_provided: "사용자 등록", observed: "실행 기록 연결", verified: "실행 기록 연결", verified_tool: "실행 기록 연결", guided: "안내형 절차", unvalidated: "실행 미검증" };
  const date = (value) => value ? new Date(value).toLocaleString("ko-KR") : "—";
  const items = (data) => Array.isArray(data) ? data : (data?.items || []);
  const caseData = (data) => data?.case ? { ...data.case, ...data, case: undefined } : data;
  const ev = (e) => ({ ...e, ...(e.current || e.current_version_data || (typeof e.version === "object" ? e.version : {})), evidence_id: e.evidence_id || e.id, version: typeof e.version === "number" ? e.version : (e.current?.version || e.current_version_data?.version || e.version?.version || e.current_version || 1) });
  const sourceRefs = () => state.current?.refs || state.current?.source_refs || [];
  const evidence = () => (state.current?.evidence || []).map(ev);
  const selectedValues = (form, name) => Array.from(form.querySelectorAll(`input[name="${name}"]:checked`)).map((input) => input.value);

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }
  function button(text, handler, className = "secondary") {
    const element = node("button", text, className);
    element.type = "button";
    element.addEventListener("click", () => Promise.resolve().then(handler).catch(showError));
    return element;
  }
  function badge(value) { return node("span", labels[value] || value || "—", `badge ${/^[a-z_]+$/.test(value || "") ? value : ""}`); }
  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").classList.toggle("error", error);
    $("notice").hidden = !message;
  }
  function showError(error) { notice(error.message || "요청을 처리하지 못했습니다.", true); }
  function expire() {
    state.token = null;
    clearTimeout(state.poll);
    $("session-status").textContent = "접근 시간 종료";
    $("session-ended").hidden = false;
    $("workspace").hidden = true;
    $("refresh").disabled = true;
    if ($("dialog").open) $("dialog").close();
  }
  function remaining() {
    if (!state.token) return;
    const seconds = Math.ceil((state.expiresAt - Date.now()) / 1000);
    if (seconds <= 0) { expire(); return; }
    $("session-status").textContent = `접근 시간 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }
  function requestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  async function request(path, { method = "GET", body, key, blob = false, publicRequest = false } = {}) {
    if (!publicRequest && (!state.token || Date.now() >= state.expiresAt)) { expire(); throw new Error("접근 시간이 끝났습니다. Open WebUI에서 검토 화면을 다시 열어 주세요."); }
    const headers = { Accept: blob ? "*/*" : "application/json" };
    if (!publicRequest) headers.Authorization = `Bearer ${state.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET" && !publicRequest) headers["Idempotency-Key"] = key || requestId();
    let response;
    try { response = await fetch(`${API}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "omit", cache: "no-store", redirect: "error" }); }
    catch (_) { throw new Error("서버에 연결할 수 없습니다. 저장 여부를 새로고침으로 확인한 뒤 다시 시도해 주세요."); }
    if (response.ok && blob) return response.blob();
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && !publicRequest) expire();
      const detail = data.error || {};
      const suffix = response.status === 409 ? " 최신 기록을 새로고침하고 변경 내용을 확인해 주세요." : "";
      const error = new Error(`${detail.message || "요청을 처리하지 못했습니다."}${suffix}${data.request_id ? ` (요청 ${data.request_id})` : ""}`);
      error.status = response.status;
      throw error;
    }
    return data.data;
  }
  async function mutate(path, payload, success) {
    if (state.pending) throw new Error("이전 요청 처리 중입니다. 잠시 후 다시 시도해 주세요.");
    state.pending = true;
    document.body.classList.add("busy");
    try {
      // Keep a key only while the write outcome is uncertain, so retrying a lost
      // response cannot create another proposal. Never replay a source Tool here.
      const fingerprint = `${path}:${JSON.stringify(payload)}`;
      if (!state.keys.has(fingerprint)) state.keys.set(fingerprint, requestId());
      const data = await request(path, { method: "POST", body: payload, key: state.keys.get(fingerprint) });
      state.keys.delete(fingerprint);
      try {
        if (state.current) await loadCase(state.current.id);
        await loadCases();
        notice(success || "저장했습니다.");
      } catch (error) { notice(`저장했습니다. 최신 화면을 불러오지 못했습니다: ${error.message}`, true); }
      return data;
    } finally { state.pending = false; document.body.classList.remove("busy"); }
  }
  function details(title, value) {
    const element = node("details");
    element.append(node("summary", title), node("pre", typeof value === "string" ? value : JSON.stringify(value, null, 2)));
    return element;
  }
  function heading(title, help, buttons = []) {
    const element = node("div", null, "panel-head");
    const text = node("div"); text.append(node("h2", title), node("p", help));
    const actions = node("div", null, "actions"); buttons.forEach((b) => actions.append(b));
    element.append(text, actions); return element;
  }
  function empty(text) { return node("p", text, "empty-inline"); }
  function readonly() { return state.current?.lifecycle !== "active" || !!state.current?.deleted_at; }
  function field(form, label, name, { type = "text", value = "", required = true, maxLength, help, options = [] } = {}) {
    const wrap = node("div", null, "field");
    const caption = node("label", label);
    const input = document.createElement(type === "textarea" ? "textarea" : type === "select" ? "select" : "input");
    input.id = `field-${requestId()}`; caption.htmlFor = input.id;
    input.name = name;
    if (!["textarea", "select"].includes(type)) input.type = type;
    input.required = required;
    if (maxLength) input.maxLength = maxLength;
    options.forEach(([optionValue, text]) => { const option = node("option", text); option.value = optionValue; input.append(option); });
    input.value = value;
    wrap.append(caption, input);
    if (help) { const description = node("small", help); description.id = `${input.id}-help`; input.setAttribute("aria-describedby", description.id); wrap.append(description); }
    form.append(wrap); return input;
  }
  function checks(form, label, name, entries, selected = []) {
    const wrap = node("fieldset", null, "field");
    wrap.append(node("legend", label));
    const options = node("div", null, "check-options");
    entries.forEach(([value, text]) => {
      const option = node("label", null, "check-option"); const input = node("input");
      input.type = "checkbox"; input.name = name; input.value = value; input.checked = selected.includes(value);
      option.append(input, node("span", text)); options.append(option);
    });
    if (!entries.length) options.append(node("p", "선택할 자료가 없습니다.", "form-help"));
    wrap.append(options); form.append(wrap);
  }
  function openForm(title, build, submitLabel, onSubmit) {
    $("dialog-title").textContent = title; $("dialog-body").replaceChildren();
    const form = node("form");
    build(form);
    const error = node("p", "", "form-error"); error.hidden = true;
    const actions = node("div", null, "form-actions");
    const submit = node("button", submitLabel); submit.type = "submit";
    actions.append(button("취소", () => $("dialog").close()), submit); form.append(error, actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (submit.disabled) return;
      submit.disabled = true; error.hidden = true;
      try { await onSubmit(new FormData(form), form); $("dialog").close(); }
      catch (err) { error.textContent = err.message; error.hidden = false; }
      finally { submit.disabled = false; }
    });
    $("dialog-body").append(form); $("dialog").showModal();
  }
  function sourceChoices() { return sourceRefs().map((r) => [r.id, `${r.label || r.id} · ${labels[r.provenance_status] || r.provenance_status}`]); }
  function sourceView(ref) {
    const card = node("article", null, "card");
    const top = node("div", null, "card-top"); top.append(node("h3", ref.label || "원천 자료"), badge(ref.provenance_status));
    card.append(top, node("p", `${date(ref.created_at)}${ref.shareable ? " · 공유 가능한 자료" : " · 공유 범위 제한"}`, "source-caption"));
    card.append(details("원문 확인", ref.content), details("출처 · 실행 정보", { ref_id: ref.id, run_id: ref.run_id, call_id: ref.call_id, locator: ref.locator, content_hash: ref.content_hash }));
    return card;
  }
  async function loadCases() {
    const suffix = state.context.chat_id ? `?chat_id=${encodeURIComponent(state.context.chat_id)}` : "";
    state.cases = items(await request(`/cases${suffix}`)).map(caseData);
    $("new-case").hidden = !!state.context.case_id || (!!state.context.chat_id && state.cases.length > 0);
    renderCases();
  }
  function renderCases() {
    const list = $("case-list"); list.replaceChildren();
    const query = $("case-search").value.trim().toLowerCase();
    const cases = state.cases.filter((c) => c.title.toLowerCase().includes(query));
    cases.forEach((c) => {
      const item = button("", () => loadCase(c.id), "case-item");
      item.setAttribute("aria-current", String(c.id === state.current?.id));
      item.append(node("strong", c.title), node("span", `${labels[c.lifecycle] || c.lifecycle} · ${date(c.updated_at || c.created_at)}`)); list.append(item);
    });
    if (!cases.length) list.append(node("p", query ? "일치하는 업무가 없습니다." : "아직 연결된 업무가 없습니다.", "subtle"));
  }
  async function loadCase(id) {
    clearTimeout(state.poll);
    const generation = ++state.generation;
    const current = caseData(await request(`/cases/${encodeURIComponent(id)}`));
    const assets = items(await request(`/assets?case_id=${encodeURIComponent(id)}&include_expired=true&limit=100`));
    if (generation !== state.generation) return;
    state.current = current; state.assets = assets;
    renderCases(); renderCase();
    if ((state.current.reports || []).some((r) => ["queued", "running"].includes(r.state))) {
      const poll = () => {
        if (!state.token || state.current?.id !== id) return;
        // Do not replace a revision while the person is reviewing a form.
        if ($("dialog").open || state.pending) { state.poll = setTimeout(poll, 4000); return; }
        loadCase(id).catch(showError);
      };
      state.poll = setTimeout(poll, 4000);
    }
  }
  function renderCase() {
    const c = state.current;
    $("empty-state").hidden = !!c; $("case-workspace").hidden = !c;
    if (!c) return;
    $("case-title").textContent = c.title; $("case-purpose").textContent = c.purpose;
    $("case-meta").replaceChildren(badge(c.lifecycle), node("span", `수정 ${date(c.updated_at)} · 기록 ${c.revision}`));
    const actions = $("case-actions"); actions.replaceChildren();
    if (c.lifecycle === "completed" || c.lifecycle === "archived") actions.append(button("업무 다시 열기", () => reasonChange("reopen", "업무 다시 열기")));
    if (c.lifecycle === "active") actions.append(button("보관", () => reasonChange("archive", "업무 보관"), "quiet"));
    const stats = $("case-stats"); stats.replaceChildren();
    [[sourceRefs().length, "원천 자료"], [evidence().filter((e) => e.review_status === "adopted").length, "채택 근거"], [evidence().filter((e) => ["unreviewed", "needs_review"].includes(e.review_status)).length, "검토 대기"], [state.assets.length, "절차 · 지식"]].forEach(([count, title]) => {
      const stat = node("div", null, "stat"); stat.append(node("strong", count), node("span", title)); stats.append(stat);
    });
    renderPanel();
  }
  function renderPanel() {
    const panel = $("panel"); panel.replaceChildren();
    document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-current", b.dataset.tab === state.tab ? "page" : "false"));
    if (!state.current) return;
    if (state.current.active_run_id) {
      const warning = node("div", null, "run-warning");
      warning.append(node("p", "진행 중이거나 확인이 필요한 도구 실행이 있습니다. 결과 확정 전에 실행 상태를 확인해 주세요."));
      const actions = node("div", null, "actions"); actions.append(button("실행 상태 확인", inspectRun)); warning.append(actions); panel.append(warning);
    }
    if (state.tab === "sources") renderSources(panel);
    else if (state.tab === "results") renderResults(panel);
    else if (state.tab === "assets") renderAssets(panel);
    else renderEvidence(panel);
  }
  function renderSources(panel) {
    panel.append(heading("원천 자료", "자료 원문과 수집 경로를 확인합니다. 직접 입력한 내용은 사용자 등록 자료로 구분합니다.", readonly() ? [] : [button("자료 직접 등록", importSource)]));
    const list = node("div", null, "cards"); sourceRefs().forEach((r) => list.append(sourceView(r)));
    panel.append(sourceRefs().length ? list : empty("등록된 자료가 없습니다. Open WebUI에서 기록이 연결된 도구를 실행하거나 자료를 직접 등록해 주세요."));
  }
  function importSource() {
    openForm("자료 직접 등록", (form) => {
      field(form, "자료 이름", "label", { maxLength: 200 });
      field(form, "원문", "text", { type: "textarea", maxLength: 20000, help: "수집 경로는 ‘사용자 등록’으로 기록됩니다. 이 입력으로 원천 시스템의 값은 변경되지 않습니다." });
    }, "자료 등록", async (data) => mutate(`/cases/${state.current.id}/source-imports`, { kind: "user_text", label: data.get("label").trim(), text: data.get("text").trim(), expected_revision: state.current.revision }, "자료를 등록했습니다. 근거 검토에서 근거를 작성할 수 있습니다."));
  }
  function renderEvidence(panel) {
    panel.append(heading("근거 검토", "원문과 해석을 확인한 뒤 채택합니다. 내용이 바뀐 근거는 새 버전으로 다시 검토합니다.", readonly() ? [] : [button("근거 직접 작성", () => evidenceForm())]));
    const list = node("div", null, "cards");
    evidence().forEach((e) => {
      const card = node("article", null, "card"); const top = node("div", null, "card-top");
      top.append(node("h3", e.summary), badge(e.review_status)); card.append(top);
      if (e.interpretation) card.append(node("p", e.interpretation));
      card.append(node("p", `버전 ${e.version}${e.reviewed_at ? ` · 검토 ${date(e.reviewed_at)}` : ""}`, "source-caption"));
      if (e.review_reason) card.append(node("p", `검토 사유: ${e.review_reason}`, "source-caption"));
      const refs = node("details"); refs.append(node("summary", `연결된 원문 ${e.ref_ids?.length || 0}개`));
      (e.ref_ids || []).forEach((id) => {
        const ref = sourceRefs().find((r) => r.id === id);
        refs.append(ref ? sourceView(ref) : node("p", `자료 ${id} · 원문을 확인할 수 없습니다.`));
      }); card.append(refs);
      if (!readonly()) {
        const actions = node("div", null, "actions");
        actions.append(button("채택", () => reviewEvidence(e, "adopted"), ""), button("재검토", () => reviewEvidence(e, "needs_review")), button("제외", () => reviewEvidence(e, "excluded")), button("내용 수정", () => evidenceForm(e), "quiet")); card.append(actions);
      }
      list.append(card);
    });
    panel.append(evidence().length ? list : empty("검토할 근거가 없습니다. 원천 자료를 등록한 후 근거를 작성하거나 Open WebUI에서 근거 제안을 요청해 주세요."));
  }
  function evidenceForm(existing) {
    openForm(existing ? "근거 새 버전 작성" : "근거 직접 작성", (form) => {
      field(form, "근거 요약", "summary", { value: existing?.summary || "", maxLength: 1000, type: "textarea" });
      field(form, "해석", "interpretation", { value: existing?.interpretation || "", required: false, maxLength: 2000, type: "textarea" });
      checks(form, "연결할 원천 자료 (1~8개)", "refs", sourceChoices(), existing?.ref_ids || []);
      if (existing) form.append(node("p", "새 버전은 검토 전 상태로 저장됩니다. 이 근거를 사용한 결과와 보고서는 재확인이 필요해집니다.", "form-help"));
    }, existing ? "새 버전 저장" : "근거 제안 저장", async (data, form) => {
      const refs = selectedValues(form, "refs"); if (refs.length < 1 || refs.length > 8) throw new Error("원천 자료를 1~8개 선택해 주세요.");
      const body = { ref_ids: refs, summary: data.get("summary").trim(), interpretation: data.get("interpretation").trim() || null };
      if (existing) Object.assign(body, { expected_current_version: existing.version, expected_revision: state.current.revision });
      await mutate(existing ? `/evidence/${existing.evidence_id}/versions` : `/cases/${state.current.id}/evidence/proposals`, body, "근거를 저장했습니다. 원문을 확인한 뒤 채택해 주세요.");
    });
  }
  function reviewEvidence(e, decision) {
    openForm(`근거 ${labels[decision]}`, (form) => {
      form.append(node("p", e.summary), node("p", `검토 대상: 버전 ${e.version}`, "form-help"));
      field(form, decision === "adopted" ? "검토 의견 (선택)" : "검토 사유", "reason", { type: "textarea", required: decision !== "adopted", maxLength: 2000 });
      if (e.review_status === "adopted" && decision !== "adopted") form.append(node("p", "이 근거를 사용한 결과와 보고서는 재확인이 필요해집니다.", "form-help"));
    }, labels[decision], async (data) => mutate(`/evidence/${e.evidence_id}/reviews`, { evidence_version: e.version, decision, reason: data.get("reason").trim() || null, expected_revision: state.current.revision }, `근거를 ${labels[decision]} 상태로 저장했습니다.`));
  }
  function renderResults(panel) {
    panel.append(heading("결과 · 보고서", "채택한 근거의 현재 버전을 결과에 고정합니다. 이후 수정은 기존 보고서에 자동 반영되지 않습니다.", readonly() ? [] : [button("결과 확정", acceptResult)]));
    const list = node("div", null, "cards");
    const snapshots = state.current.snapshots || [];
    snapshots.forEach((s) => {
      const card = node("article", null, "card"); const top = node("div", null, "card-top"); top.append(node("h3", s.title), badge(s.state)); card.append(top);
      if (s.narrative) card.append(node("p", s.narrative));
      card.append(node("p", `확정 ${date(s.created_at)}`, "source-caption"));
      if (s.body) card.append(details("확정한 근거와 내용", s.body));
      const actions = node("div", null, "actions");
      if (s.state === "accepted") {
        actions.append(button("Markdown 보고서", () => createReport(s, "md")), button("Word 보고서", () => createReport(s, "docx")));
        if (!readonly()) actions.append(button("이 결과로 업무 완료", () => completeCase(s), ""));
      }
      card.append(actions); list.append(card);
    });
    panel.append(snapshots.length ? list : empty("확정된 결과가 없습니다. 근거를 채택한 후 결과를 확정해 주세요."));
    panel.append(node("h2", "생성한 보고서", "section-divider"));
    const reports = state.current.reports || [];
    const reportList = node("div", null, "cards");
    reports.forEach((r) => {
      const card = node("article", null, "card"); const top = node("div", null, "card-top"); top.append(node("h3", `${r.format.toUpperCase()} 보고서`), badge(r.outdated ? "superseded" : r.state)); card.append(top);
      card.append(node("p", date(r.created_at), "source-caption"));
      if (r.outdated) card.append(node("p", "근거 또는 결과가 변경되었습니다. 현재 업무 상태를 다시 검토해 주세요."));
      if (r.error_code) card.append(node("p", `생성 실패: ${r.error_code}`));
      if (r.artifact_id) {
        const actions = node("div", null, "actions"); actions.append(button("다운로드", () => download(`/artifacts/${r.artifact_id}/content`, `quality-report-${r.id}.${r.format}`))); card.append(actions);
      }
      reportList.append(card);
    });
    panel.append(reports.length ? reportList : empty("아직 생성한 보고서가 없습니다."));
  }
  function acceptResult() {
    const adopted = evidence().filter((e) => e.review_status === "adopted");
    openForm("결과 확정", (form) => {
      field(form, "결과 제목", "title", { value: state.current.title, maxLength: 200 });
      field(form, "결과 설명", "narrative", { type: "textarea", required: false, maxLength: 10000 });
      checks(form, "포함할 채택 근거", "evidence", adopted.map((e) => [e.evidence_id, `${e.summary} · 버전 ${e.version}`]), adopted.map((e) => e.evidence_id));
      form.append(node("p", "선택한 근거의 현재 버전과 원문 참조가 확정 결과에 기록됩니다.", "form-help"));
    }, "결과 확정", async (data, form) => {
      const selected = selectedValues(form, "evidence"); if (!selected.length) throw new Error("채택한 근거를 하나 이상 선택해 주세요.");
      await mutate(`/cases/${state.current.id}/results`, { title: data.get("title").trim(), narrative: data.get("narrative").trim() || null, evidence_versions: adopted.filter((e) => selected.includes(e.evidence_id)).map((e) => ({ evidence_id: e.evidence_id, version: e.version })), expected_revision: state.current.revision }, "결과를 확정했습니다.");
    });
  }
  function completeCase(snapshot) {
    openForm("업무 완료", (form) => form.append(node("p", `‘${snapshot.title}’ 결과로 업무를 완료합니다. 이후 근거를 수정하려면 업무를 다시 열어야 합니다.`)), "업무 완료", () => mutate(`/cases/${state.current.id}/complete`, { snapshot_id: snapshot.id, expected_revision: state.current.revision }, "업무를 완료했습니다."));
  }
  function reasonChange(action, title) {
    openForm(title, (form) => field(form, "사유", "reason", { type: "textarea", maxLength: 2000 }), title, (data) => mutate(`/cases/${state.current.id}/${action}`, { reason: data.get("reason").trim(), expected_revision: state.current.revision }));
  }
  async function createReport(snapshot, format) {
    await mutate(`/snapshots/${snapshot.id}/reports`, { format, template_id: "quality-standard", template_version: 1 }, "보고서 생성을 요청했습니다. 완료되면 다운로드 버튼이 표시됩니다.");
  }
  async function download(path, filename) {
    const blob = await request(path, { blob: true }); const url = URL.createObjectURL(blob);
    const link = node("a"); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function renderAssets(panel) {
    panel.append(heading("절차 · 지식", "검토한 내용을 개인 지식으로 저장하고, 허용된 자료에 한해 팀 공유를 요청합니다.", [button("지식 초안 작성", knowledgeDraft), button("실행 기록을 절차로", procedureDraft)]));
    const list = node("div", null, "cards");
    state.assets.forEach((asset) => {
      const v = asset.version; const permissions = asset.permissions || {};
      const card = node("article", null, "card"); const top = node("div", null, "card-top"); top.append(node("h3", v.title), badge(asset.effective_status || asset.status)); card.append(top, node("p", v.summary));
      card.append(node("p", `${asset.kind === "procedure" ? "절차" : "지식"} · 버전 ${v.version} · ${v.category}`, "source-caption"));
      card.append(details("내용과 적용 조건", `${v.content_md}\n\n적용 조건\n${v.applicability}`), details("출처와 검증 상태", { ref_ids: v.ref_ids, validation_status: v.validation_status, validation_report: v.validation_report, expires_at: asset.expires_at }));
      if (asset.history?.length || asset.feedback?.length) card.append(details("버전 이력 · 피드백", { history: asset.history || [], feedback: asset.feedback || [] }));
      if (asset.kind === "procedure") card.append(node("p", "안내형 절차입니다. 원천 도구를 자동으로 다시 실행하지 않습니다.", "source-caption"));
      const actions = node("div", null, "actions");
      if (permissions.approve) actions.append(button("개인 저장 승인", () => approveAsset(asset), ""));
      if (permissions.submit) actions.append(button("팀 공유 요청", () => submitAsset(asset)));
      if (permissions.curate) actions.append(button("공유 검토", () => curateAsset(asset)));
      if (permissions.curate && asset.kind === "knowledge") actions.append(button("병합 검토", () => mergeAsset(asset)));
      if (permissions.revise) actions.append(button("새 버전 작성", () => reviseAsset(asset)));
      if (permissions.validate) actions.append(button("구조 검증", () => validateAsset(asset)));
      if (permissions.feedback) actions.append(button("피드백", () => feedbackAsset(asset)));
      if (permissions.deprecate) actions.append(button("사용 중지", () => assetLifecycle(asset, "deprecate"), "quiet"));
      if (permissions.archive) actions.append(button("보관", () => assetLifecycle(asset, "archive"), "quiet"));
      if (asset.usable || ["personal", "published"].includes(asset.effective_status)) actions.append(button("내보내기", () => download(`/assets/${asset.id}/export`, `quality-${asset.kind}-${asset.id}.zip`), "quiet"));
      card.append(actions); list.append(card);
    });
    panel.append(state.assets.length ? list : empty("저장된 절차와 지식이 없습니다. 업무에서 확인한 내용을 초안으로 정리해 보세요."));
  }
  function knowledgeDraft() {
    openForm("지식 초안 작성", (form) => {
      field(form, "제목", "title", { maxLength: 200 });
      field(form, "요약", "summary", { maxLength: 1000, type: "textarea" });
      field(form, "내용", "content_md", { type: "textarea", maxLength: 50000 });
      field(form, "분류", "category", { value: "품질 업무", maxLength: 100 });
      field(form, "적용 조건 · 범위", "applicability", { maxLength: 2000, type: "textarea" });
      checks(form, "근거 자료 (1~20개)", "refs", sourceChoices());
    }, "초안 저장", async (data, form) => {
      const refs = selectedValues(form, "refs"); if (!refs.length || refs.length > 20) throw new Error("근거 자료를 1~20개 선택해 주세요.");
      await mutate(`/cases/${state.current.id}/asset-drafts`, { kind: "knowledge", title: data.get("title").trim(), summary: data.get("summary").trim(), content_md: data.get("content_md").trim(), category: data.get("category").trim(), applicability: data.get("applicability").trim(), ref_ids: refs, proposed_expires_at: null, procedure: null }, "지식 초안을 저장했습니다. 내용을 확인한 후 개인 저장을 승인해 주세요.");
    });
  }
  function procedureDraft() {
    const runIds = [...new Set(sourceRefs().map((r) => r.run_id).filter(Boolean))];
    openForm("실행 기록을 절차로 만들기", (form) => {
      field(form, "실행 기록", "run_id", { type: "select", value: runIds[0] || "", options: runIds.map((id) => [id, id]) });
      field(form, "절차 제목", "title", { maxLength: 200, value: state.current.title });
      field(form, "절차 요약", "summary", { type: "textarea", maxLength: 1000 });
      field(form, "분류", "category", { value: "품질 업무", maxLength: 100 });
      field(form, "적용 조건 · 범위", "applicability", { type: "textarea", maxLength: 2000 });
      form.append(node("p", runIds.length ? "기록된 읽기 도구의 실행 순서를 안내형 절차로 정리합니다. 실제 재실행 검증이 완료된 자동 절차로 표시하지 않습니다." : "기록된 도구 실행이 없습니다. Open WebUI에서 추적이 연결된 읽기 도구를 먼저 사용해 주세요.", "form-help"));
    }, "절차 초안 만들기", async (data) => {
      if (!runIds.length) throw new Error("절차로 만들 실행 기록이 없습니다.");
      await mutate(`/cases/${state.current.id}/procedure-drafts`, { run_id: data.get("run_id"), title: data.get("title").trim(), summary: data.get("summary").trim(), category: data.get("category").trim(), applicability: data.get("applicability").trim() }, "안내형 절차 초안을 만들었습니다.");
    });
  }
  function assetRevision(asset) { return { expected_asset_revision: asset.revision, asset_version: asset.current_version }; }
  function reviseAsset(asset) {
    const v = asset.version;
    openForm("자산 새 버전 작성", (form) => {
      field(form, "제목", "title", { value: v.title, maxLength: 200 });
      field(form, "요약", "summary", { value: v.summary, type: "textarea", maxLength: 1000 });
      field(form, "내용", "content_md", { value: v.content_md, type: "textarea", maxLength: 50000 });
      field(form, "분류", "category", { value: v.category, maxLength: 100 });
      field(form, "적용 조건 · 범위", "applicability", { value: v.applicability, type: "textarea", maxLength: 2000 });
      checks(form, "근거 자료 (1~20개)", "refs", sourceChoices(), v.ref_ids);
      form.append(node("p", "새 버전은 초안으로 저장되며 개인 저장과 팀 공유 검토를 다시 거칩니다. 절차의 도구 실행 순서를 바꾸려면 새 실행 기록에서 절차 초안을 만들어 주세요.", "form-help"));
    }, "새 버전 저장", async (data, form) => {
      const refs = selectedValues(form, "refs"); if (!refs.length || refs.length > 20) throw new Error("근거 자료를 1~20개 선택해 주세요.");
      await mutate(`/assets/${asset.id}/versions`, { expected_asset_revision: asset.revision, expected_current_version: asset.current_version, kind: asset.kind, title: data.get("title").trim(), summary: data.get("summary").trim(), content_md: data.get("content_md").trim(), category: data.get("category").trim(), applicability: data.get("applicability").trim(), ref_ids: refs, proposed_expires_at: null, procedure: v.procedure || null }, "새 버전 초안을 저장했습니다.");
    });
  }
  function approveAsset(asset) {
    openForm("개인 저장 승인", (form) => {
      form.append(node("p", `‘${asset.version.title}’ 버전 ${asset.current_version}을 검토하고 개인 자산으로 저장합니다.`));
      field(form, "만료일 (선택)", "expires_at", { type: "datetime-local", required: false, help: "선택한 시간은 현재 브라우저 시간대를 기준으로 저장됩니다." });
    }, "개인 저장 승인", (data) => mutate(`/assets/${asset.id}/approve`, { ...assetRevision(asset), content_hash: asset.version.content_hash, expires_at: data.get("expires_at") ? new Date(data.get("expires_at")).toISOString() : null }));
  }
  function submitAsset(asset) {
    const teams = state.context.team_ids || [];
    openForm("팀 공유 요청", (form) => {
      field(form, "공유할 팀", "team_id", { type: teams.length ? "select" : "text", options: teams.map((id) => [id, id]), value: teams[0] || "", help: "서버에 등록된 소속 팀과 자료의 공유 가능 여부를 검사합니다." });
      form.append(node("p", `‘${asset.version.title}’ 버전 ${asset.current_version}을 팀 공유 검토로 제출합니다.`, "form-help"));
    }, "공유 요청", (data) => mutate(`/assets/${asset.id}/submit`, { ...assetRevision(asset), target_team_id: data.get("team_id") }));
  }
  function curateAsset(asset) {
    openForm("팀 공유 검토", (form) => {
      form.append(node("p", `${asset.version.title} · 버전 ${asset.current_version}`));
      field(form, "검토 결정", "action", { type: "select", value: "publish", options: [["publish", "팀에 게시"], ["reject", "개인 자산으로 반려"]] });
      field(form, "검토 사유", "reason", { type: "textarea", maxLength: 2000 });
    }, "검토 결정 저장", (data) => mutate(`/assets/${asset.id}/curation-decisions`, { ...assetRevision(asset), action: data.get("action"), reason: data.get("reason").trim(), merge_target_id: null }));
  }
  function validateAsset(asset) { return mutate(`/assets/${asset.id}/validate`, { ...assetRevision(asset), fixture_ids: [] }, "구조를 검증했습니다. 실행 검증 여부는 자산의 검증 상태에서 별도로 확인해 주세요."); }
  function assetLifecycle(asset, action) {
    const title = action === "archive" ? "자산 보관" : "자산 사용 중지";
    openForm(title, (form) => {
      form.append(node("p", `‘${asset.version.title}’ 버전 ${asset.current_version}을 변경합니다.`));
      field(form, "사유", "reason", { type: "textarea", maxLength: 2000 });
    }, title, (data) => mutate(`/assets/${asset.id}/lifecycle`, { ...assetRevision(asset), action, reason: data.get("reason").trim() }));
  }
  async function mergeAsset(asset) {
    const suggestions = items(await request(`/assets/${asset.id}/curation-suggestions`));
    openForm("지식 병합안 만들기", (form) => {
      field(form, "병합 대상", "target", { type: "select", value: suggestions[0]?.asset_id || "", options: suggestions.map((s) => [s.asset_id, `${s.title} · 버전 ${s.asset_version}`]) });
      field(form, "병합 검토 사유", "reason", { type: "textarea", maxLength: 2000 });
      form.append(node("p", suggestions.length ? "후보는 문구 유사도로 제안됩니다. 내용의 충돌 여부는 사람이 확인해야 합니다. 같은 팀에 게시된 지식만 병합 대상으로 사용할 수 있습니다." : "접근 가능한 병합 후보가 없습니다.", "form-help"));
    }, "병합안 확인", async (data) => {
      if (!data.get("target")) throw new Error("병합할 대상이 없습니다.");
      const result = await mutate(`/assets/${asset.id}/curation-decisions`, { ...assetRevision(asset), action: "propose_merge", reason: data.get("reason").trim(), merge_target_id: data.get("target") }, "병합안을 만들었습니다. 변경 내용을 확인해 주세요.");
      setTimeout(() => confirmMerge(asset, result.proposal), 0);
    });
  }
  function confirmMerge(asset, proposal) {
    if (!state.token || !proposal) return;
    openForm("병합 내용 최종 확인", (form) => {
      form.append(node("p", proposal.content.title), details("병합 후 전체 내용", proposal.content.content_md));
      form.append(node("p", "승인하면 병합 대상의 새 초안을 만들고 원래 지식은 사용 중지합니다. 대상 소유자가 새 버전을 개인 저장·공유 검토한 후에 다시 게시할 수 있습니다.", "form-help"));
      checks(form, "변경 내용 확인", "confirm", [["yes", "병합 후 전체 내용과 대상 버전을 확인했습니다."]]);
    }, "병합 승인", async (_data, form) => {
      if (!selectedValues(form, "confirm").length) throw new Error("병합 내용 확인 항목을 선택해 주세요.");
      const body = {};
      for (const key of ["target_asset_id", "target_asset_version", "source_asset_versions", "proposed_content_hash", "expected_target_revision", "expected_source_revisions"]) body[key] = proposal[key];
      await mutate(`/assets/${asset.id}/merge-confirmations`, body, "병합한 새 초안을 저장했습니다. 대상 소유자의 검토가 필요합니다.");
    });
  }
  function feedbackAsset(asset) {
    openForm("자산 피드백", (form) => {
      field(form, "종류", "kind", { type: "select", value: "useful", options: [["useful", "도움이 되었음"], ["incorrect", "수정이 필요함"]] });
      field(form, "설명", "comment", { type: "textarea", maxLength: 2000, required: false, help: "수정이 필요하면 문제가 발생한 조건과 내용을 적어 주세요." });
    }, "피드백 저장", (data) => {
      if (data.get("kind") === "incorrect" && !data.get("comment").trim()) throw new Error("수정이 필요한 조건과 설명을 입력해 주세요.");
      return mutate(`/assets/${asset.id}/feedback`, { asset_version: asset.current_version, kind: data.get("kind"), comment: data.get("comment").trim() || null });
    });
  }
  async function inspectRun() {
    const run = await request(`/runs/${state.current.active_run_id}`);
    const calls = run.calls || []; const unresolved = calls.filter((c) => ["claimed", "unknown"].includes(c.state) && c.disposition !== "excluded_by_human");
    openForm("도구 실행 상태", (form) => {
      form.append(node("p", `실행 상태: ${labels[run.state] || run.state}`));
      calls.forEach((call) => form.append(details(`${call.tool_id} · ${labels[call.state] || call.state}`, { arguments: call.arguments, upstream_request_id: call.upstream_request_id, error_code: call.error_code, disposition: call.disposition })));
      if (unresolved.length) checks(form, "결과에서 제외할 미확인 읽기 실행", "calls", unresolved.map((c) => [c.id, `${c.tool_id} · ${labels[c.state] || c.state}`]));
      field(form, unresolved.length ? "제외 사유" : "중단 확인 사유", "reason", { type: "textarea", maxLength: 2000 });
      form.append(node("p", unresolved.length ? "이 조작은 외부 실행을 취소하지 않습니다. 확인되지 않은 읽기 결과를 업무 결과에서 제외한 기록을 남깁니다." : "미확인 도구 호출이 없습니다. 추가 결과를 기다리지 않고 이 실행을 중단 상태로 정리합니다. 외부 실행 취소 요청은 전송하지 않습니다.", "form-help"));
    }, unresolved.length ? "선택한 읽기 결과 제외" : "실행 중단 확인", async (data, form) => {
      const selected = selectedValues(form, "calls"); if (unresolved.length && !selected.length) throw new Error("제외할 미확인 읽기 실행을 선택해 주세요.");
      await mutate(`/runs/${state.current.active_run_id}/resolve-read`, { action: "exclude_unresolved_read", call_ids: selected, reason: data.get("reason").trim(), expected_revision: state.current.revision });
    });
  }
  function newCase() {
    openForm("새 업무 시작", (form) => {
      field(form, "업무 제목", "title", { maxLength: 200 });
      field(form, "업무 목적", "purpose", { type: "textarea", maxLength: 2000 });
      field(form, "업무 유형", "profile", { type: "select", value: state.profiles[0]?.id || "", options: state.profiles.map((p) => [p.id, p.name || p.title || p.id]) });
      form.append(node("p", "Open WebUI에서 이 화면을 연 대화에 연결됩니다.", "form-help"));
    }, "업무 만들기", async (data) => {
      const profile = state.profiles.find((p) => p.id === data.get("profile")); if (!profile) throw new Error("등록된 업무 유형을 선택해 주세요.");
      const created = caseData(await mutate("/cases", { title: data.get("title").trim(), purpose: data.get("purpose").trim(), profile_id: profile.id, profile_version: profile.version }, "업무를 만들었습니다."));
      await loadCase(created.id);
    });
  }
  async function boot() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const ticket = fragment.get("ticket");
    history.replaceState(null, "", window.location.pathname + window.location.search);
    $("dialog-close").addEventListener("click", () => $("dialog").close());
    $("case-search").addEventListener("input", renderCases);
    $("new-case").addEventListener("click", newCase);
    $("refresh").addEventListener("click", () => Promise.resolve().then(async () => { if (state.current) await loadCase(state.current.id); await loadCases(); notice("최신 기록을 불러왔습니다."); }).catch(showError));
    document.querySelectorAll("[data-tab]").forEach((tab) => tab.addEventListener("click", () => { state.tab = tab.dataset.tab; renderPanel(); }));
    if (!ticket) { expire(); notice("Open WebUI의 ‘품질 업무 검토’ 버튼으로 접속해 주세요."); return; }
    try {
      const exchanged = await request("/ui/exchange", { method: "POST", body: { ticket }, publicRequest: true });
      if (!exchanged?.access_token) throw new Error("접근 권한을 확인하지 못했습니다.");
      state.token = exchanged.access_token;
      state.expiresAt = Date.now() + Math.min(Number(exchanged.expires_in) || 300, 300) * 1000;
      state.context = exchanged.context || exchanged;
      $("workspace").hidden = false; $("refresh").disabled = false;
      $("new-case").hidden = !!state.context.case_id;
      remaining(); setInterval(remaining, 1000);
      state.profiles = items(await request("/profiles")).map((p) => ({ ...p, id: p.profile_id, version: p.profile_version, name: p.name || (p.profile_id === "quality-general" ? "일반 품질 업무" : p.profile_id) }));
      await loadCases();
      const initial = state.context.case_id || state.cases[0]?.id;
      if (initial) await loadCase(initial);
    } catch (error) { if (!state.token) expire(); showError(error); }
  }
  // A newly issued Action link can reopen an existing tab via hash navigation.
  // Reload to exchange that ticket in a fresh in-memory session.
  window.addEventListener("hashchange", () => {
    if (new URLSearchParams(window.location.hash.slice(1)).has("ticket")) window.location.reload();
  });
  boot();
})();
