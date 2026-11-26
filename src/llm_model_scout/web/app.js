(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const pageSize = 50;
  const labels = {queued: "排队中", running: "运行中", cancelling: "正在取消", completed: "执行完成", failed: "执行失败", cancelled: "已取消", interrupted: "执行中断", available: "通过", unavailable: "未通过", skipped: "未执行"};
  const protocols = {openai_chat: "OpenAI Chat", anthropic_messages: "Anthropic Messages", openai_responses: "OpenAI Responses"};
  const capabilityLabels = {text: "非流式文本", stream: "流式文本", structured: "结构化输出", tool: "工具调用"};
  const protocolLabel = (protocol) => protocols[protocol] || protocol;
  const capabilityLabel = (capability) => capabilityLabels[capability || "text"] || capability;
  const capabilitySuffix = (capability) => capability && capability !== "text" ? ` · ${capabilityLabel(capability)}` : "";
  const diagnosticLabels = {
    UNAUTHORIZED: "鉴权失败，请核对入口的 API Key。",
    RATE_LIMITED: "服务商返回限流，请检查配额或稍后再试。",
    TIMEOUT: "请求超时，请核对网络和超时设置。",
  };
  const diagnosticCodeLabels = {
    refusal: "模型拒绝了探测请求。",
    empty_text: "接口返回成功，但没有可验证的文本回复。",
    incomplete_response: "响应未完成。",
    response_error: "接口返回了响应错误。",
    invalid_structure: "返回结构不符合所选探测协议。",
    non_text_output: "响应包含其他输出，但没有可验证的文本回复。",
    empty_stream: "空流。服务商没有返回可验证的文本。",
    stream_interrupted: "流式响应中断，未收到协议完成事件。",
    stream_limit_exceeded: "流式响应超出大小上限。",
    invalid_stream: "流式响应格式无效。",
    invalid_structured_json: "模型输出无法解析为有效 JSON。",
    schema_mismatch: "模型输出不符合固定 JSON Schema。",
    no_tool_call: "响应中没有工具调用指令。",
    unexpected_tool_name: "返回的工具名与探测要求不符。",
    invalid_tool_arguments: "工具调用的参数格式或字段不符合要求。",
    unexpected_tool_call_count: "工具调用数量与探测要求不符。",
  };
  const budgetSlots = {scan: "create-scan", selected: "catalog-test", all: "catalog-test-all"};
  const finishedTasks = new Set(["completed", "failed", "cancelled", "interrupted"]);
  const tested = new Set(["available", "unavailable"]);
  const verificationChanges = {
    regressed: ["通过转为未通过", "bad"], recovered: ["未通过转为通过", "good"],
    unchanged: ["结果不变", ""], conditions_changed: ["探测条件变化", "warn"],
    conditions_unknown: ["探测条件无法确认", "warn"],
  };
  const catalogCoverage = {complete: "完整目录", partial: "部分目录", manual: "手选范围", missing: "无目录记录"};
  const comparisonFields = {
    "endpoint.base_url": "入口地址", "endpoint.auth": "鉴权方式",
    "endpoint.api_version": "协议版本", "endpoint.uses_proxy": "启用代理",
    provider_revision: "服务商配置修订", "probe.profile_id": "探测配置", "probe.max_tokens": "Token 上限",
    "probe.capability": "探测能力",
    "probe.temperature": "温度", "probe.prompt": "探测提示",
    "policy.timeout": "请求超时", "policy.retries": "重试次数", "policy.max_response_bytes": "响应大小上限",
    "policy.scan_timeout": "任务截止时间",
  };
  const retestState = () => ({
    models: new Set(), requestId: "", confirmation: null, changes: [], pending: false, message: "", error: false,
  });
  const comparisonState = (generation = 0) => ({
    baseline: null, current: null, endpoint: "", endpoints: [], data: null, error: "", loading: false, generation,
    retest: retestState(),
  });
  const state = {token: "", connected: false, session: 0, selection: 0, selectedId: null,
    providers: [], jobs: [], offset: 0, total: 0, taskProvider: "", taskQuery: 0, current: null, report: null,
    reportId: null, timer: null, refreshing: null, creating: false, offline: false,
    requests: new Set(), downloads: new Set(), comparison: comparisonState(), previews: {}};

  let providerUI = null;
  let managementEnabled = false;

  class ApiError extends Error {
    constructor(status, message, details = null) { super(message); this.status = status; this.details = details; }
  }
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").className = error ? "notice error" : "notice";
    $("notice").hidden = !message;
  }
  function abortRequests(tag) {
    for (const request of state.requests) {
      if (!tag || request.tag === tag) request.controller.abort();
    }
  }
  function disconnect(message) {
    state.session += 1;
    state.selection += 1;
    clearTimeout(state.timer);
    abortRequests();
    providerUI?.clear();
    managementEnabled = false;
    for (const url of state.downloads) URL.revokeObjectURL(url);
    state.downloads.clear();
    Object.assign(state, {token: "", connected: false, selectedId: null, current: null,
      report: null, reportId: null, providers: [], jobs: [], creating: false, offset: 0, total: 0,
      taskProvider: "", taskQuery: 0, refreshing: null});
    $("scan-form").reset();
    $("provider").replaceChildren(); $("endpoint").replaceChildren();
    updateScanMode();
    clearComparison();
    clearReport(); renderDetail();
    $("agent-token").value = "";
    $("workspace").hidden = true;
    $("login-panel").hidden = false;
    $("logout").hidden = true;
    $("connection-state").textContent = "未连接";
    $("task-list").replaceChildren();
    $("task-filter").hidden = true;
    fillOptions($("task-provider"), [["", "全部服务商"]], "");
    $("result-rows").replaceChildren();
    notice(message);
  }
  async function api(path, {method = "GET", body, tag = "general", blob = false} = {}) {
    const generation = state.session;
    const controller = new AbortController();
    const request = {controller, tag};
    state.requests.add(request);
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 12000);
    try {
      const headers = {Authorization: `Bearer ${state.token}`};
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal, credentials: "omit", cache: "no-store"});
      if (generation !== state.session) throw new DOMException("Superseded", "AbortError");
      if (response.status === 401) {
        disconnect("会话 Token 无效或已失效，请重新连接。退出页面不会取消后台任务。");
        throw new ApiError(401, "需要重新连接");
      }
      if (!response.ok) {
        const messages = {403: "请求来源不被允许，请使用 Agent 的本地地址。", 404: "目标不存在或已被淘汰。", 409: "状态冲突，请刷新；可能存在运行中任务或配置已改变。", 413: "模型列表过大，请缩小扫描范围。", 422: "参数不符合服务端限制，请检查输入。", 429: "任务容量已满，请等待已有任务完成。", 503: "Agent 暂时无法处理此请求。"};
        const details = tag === "retest" && response.status === 409 ? await response.json().catch(() => null) : null;
        throw new ApiError(response.status, messages[response.status] || `Agent 返回 HTTP ${response.status}。`, details);
      }
      const value = blob ? await response.blob() : await response.json();
      if (generation !== state.session) throw new DOMException("Superseded", "AbortError");
      return value;
    } catch (error) {
      if (generation !== state.session) throw new DOMException("Superseded", "AbortError");
      if (timedOut) throw new Error("请求超时，请检查 Agent 是否仍在运行。创建请求超时后先刷新列表，避免重复提交。");
      if (error instanceof TypeError) throw new Error("无法连接 Agent，请检查本地服务。后台任务可能仍在运行。");
      throw error;
    } finally {
      clearTimeout(timeout);
      state.requests.delete(request);
    }
  }
  function showError(error) {
    if (error.name !== "AbortError" && error.status !== 401) notice(error.message || "操作未完成。", true);
  }
  function badge(status) {
    const tone = status === "available" || status === "completed" ? "good" :
      status === "unavailable" || status === "failed" ? "bad" :
      status === "running" || status === "queued" ? "active" : "warn";
    return node("span", labels[status] || status, `badge ${tone}`);
  }
  function dateText(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", {hour12: false});
  }
  function fillOptions(select, entries, selected) {
    select.replaceChildren(...entries.map(([value, text]) => {
      const option = node("option", text); option.value = value; return option;
    }));
    if (entries.some(([value]) => value === selected)) select.value = selected;
  }
  function selectedProvider() { return state.providers.find((p) => p.provider_id === $("provider").value); }
  function taskSettings(payload) {
    const settings = {...payload};
    if (payload.operation !== "models") settings.capability = $("capability").value;
    for (const [id, field] of [["max-tokens", "max_tokens"], ["max-requests", "max_requests"], ["scan-timeout", "scan_timeout"]]) {
      if (!$(id).disabled && $(id).value !== "") settings[field] = Number($(id).value);
    }
    if ($("profile").value) settings.profile = $("profile").value;
    return settings;
  }
  function scanDraft() {
    const provider = selectedProvider();
    if (!provider) return {hint: "选择服务商后可预览预算。"};
    if (!$("endpoint").value) return {hint: "选择要验证的协议入口。"};
    const mode = $("scan-mode").value;
    const payload = {provider_id: provider.provider_id, endpoint_id: $("endpoint").value, mode};
    if (mode === "quick") {
      const model = $("quick-model").value.trim();
      if (!model || [...model].length > 256) return {hint: "填写 1 个模型 ID，最多 256 字符。"};
      payload.models = [model];
    } else if ($("model-mode").value === "manual") {
      const models = [...new Set($("models").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean))];
      if (!models.length || models.length > provider.limits.max_models || models.some((m) => [...m].length > 256)) {
        return {hint: `请输入 1—${provider.limits.max_models} 个模型，每个 ID 最多 256 字符。`};
      }
      payload.models = models;
    }
    if (!$("scan-form").checkValidity()) return {hint: "请核对 Token 上限、请求预算和任务截止时间。"};
    return {payload: taskSettings(payload)};
  }
  function updatePreviewButton(slot) {
    const data = state.previews[slot]?.data;
    $(budgetSlots[slot]).disabled = state.creating || !data || data.effective_max_requests <= 0;
  }
  function renderPreview(slot) {
    const preview = state.previews[slot];
    const target = $(slot + "-preview");
    updatePreviewButton(slot);
    if (!preview?.data) {
      target.textContent = preview?.error || preview?.hint || "正在计算预算…";
      target.dataset.error = String(Boolean(preview?.error));
      return;
    }
    const data = preview.data;
    const count = data.target_count == null ? `待发现（最多 ${data.max_models} 项）` : `${data.target_count} 项`;
    const endpoints = node("ul");
    endpoints.append(...data.endpoints.map((endpoint) => node("li",
      `${endpoint.id} · ${protocolLabel(endpoint.protocol)} · Token 上限 ${endpoint.max_tokens}`)));
    target.dataset.error = "false";
    target.replaceChildren(
      node("p", `验证目标 ${count} · ${capabilityLabel(data.capability)}`),
      endpoints,
      node("p", `任务预算 ${data.max_requests} 次 · 服务商剩余 ${data.requests_remaining} 次`),
      node("p", `本次最多 ${data.request_upper_bound} 次请求 · 重试 ${data.retries} 次`),
    );
  }
  function updatePreview(slot, {payload, hint} = {}) {
    if (!state.connected) payload = null;
    const provider = state.providers.find((p) => p.provider_id === payload?.provider_id);
    if (payload && new TextEncoder().encode(JSON.stringify(payload)).length > 65536) {
      payload = null; hint = "模型列表超出请求体限制，请减少模型数量。";
    }
    const key = JSON.stringify([payload, hint, provider]);
    const previous = state.previews[slot];
    if (previous?.key === key && !previous.error) { updatePreviewButton(slot); return; }
    clearTimeout(previous?.timer);
    const tag = "budget-" + slot;
    abortRequests(tag);
    const preview = {payload, key, hint, data: null, error: "", timer: null};
    state.previews[slot] = preview;
    renderPreview(slot);
    if (!payload) return;
    preview.timer = setTimeout(async () => {
      try {
        const data = await api("/scans/preview", {method: "POST", body: payload, tag});
        if (state.previews[slot] === preview) preview.data = data;
      } catch (error) {
        if (error.name !== "AbortError" && error.status !== 401) preview.error = error.message;
      } finally {
        if (state.previews[slot] === preview) renderPreview(slot);
      }
    }, 160);
  }
  function updateCatalogPreviews() {
    const batches = providerUI?.batchRequests();
    for (const slot of ["selected", "all"]) {
      const payload = batches?.[slot];
      updatePreview(slot, payload ? {payload: taskSettings(payload)} :
        {hint: batches ? "勾选要验收的模型。" : "请选择该目录对应的服务商。"});
    }
  }
  function updatePreviews() {
    $("tool-capability-note").hidden = $("capability").value !== "tool";
    updatePreview("scan", scanDraft());
    updateCatalogPreviews();
  }
  function updateScanMode() {
    const quick = $("scan-mode").value === "quick";
    $("quick-model-field").hidden = !quick;
    $("quick-model").disabled = !quick;
    $("batch-model-source").hidden = quick;
    $("manual-models").hidden = quick || $("model-mode").value !== "manual";
    $("request-budget-field").hidden = quick;
    $("max-requests").disabled = quick;
    $("discover-models").hidden = quick;
    $("create-scan").textContent = quick ? "验证指定模型" : "创建扫描任务";
    updateEndpoints();
  }
  function updateLimits() {
    const provider = selectedProvider();
    providerUI?.update(managementEnabled);
    if (!provider) {
      $("provider-info").textContent = managementEnabled ? "点击新增服务商，直接输入地址和Key，无需配置文件。" : "当前为配置文件兼容模式，请检查启动配置。";
      updatePreviews();
      return;
    }
    const endpoint = provider.endpoints.find((e) => e.id === $("endpoint").value);
    const profile = $("profile").value;
    const tokens = profile ? (profile === "baseline" ? 1 : 16) : endpoint?.probe?.max_tokens;
    $("max-tokens").placeholder = tokens ? `默认 ${tokens}` : "沿用各入口配置";
    $("max-tokens").max = provider.limits.max_tokens;
    $("max-requests").max = provider.max_requests;
    $("max-requests").placeholder = `上限 ${provider.max_requests}`;
    $("scan-timeout").max = provider.limits.scan_timeout;
    $("scan-timeout").placeholder = `默认 ${provider.limits.scan_timeout}`;
    $("provider-info").textContent = endpoint ? `${protocolLabel(endpoint.protocol)} · ${endpoint.probe.profile_id} · ${endpoint.has_manual_models ? "配置含手动候选" : "默认自动发现"}` : "全部入口独立验证，共享服务商限额。";
    updatePreviews();
  }
  function updateEndpoints() {
    const provider = selectedProvider();
    const endpoints = provider?.endpoints || [];
    const entries = endpoints.map((e) => [e.id, `${e.id} · ${protocolLabel(e.protocol)}`]);
    if (endpoints.length > 1) {
      if ($("scan-mode").value === "quick") entries.unshift(["", "选择单个协议入口"]);
      else entries.push(["all", "全部入口（分别验证）"]);
    }
    fillOptions($("endpoint"), entries, $("endpoint").value);
    updateLimits();
  }
  function applyInventory(providers) {
    if (!Array.isArray(providers)) throw new Error("服务端配置格式不符合预期，请检查版本。");
    const oldSignature = JSON.stringify(state.providers.map((p) => [p.provider_id, p.name, p.revision, p.endpoints.map((e) => e.id)]));
    state.providers = providers;
    const signature = JSON.stringify(providers.map((p) => [p.provider_id, p.name, p.revision, p.endpoints.map((e) => e.id)]));
    if (oldSignature !== signature || !$("provider").options.length) {
      fillOptions($("provider"), providers.map((p) => [p.provider_id, p.name || p.provider_id]), $("provider").value);
      updateEndpoints();
    } else updateLimits();
  }
  function providerTitle(job) {
    return job.provider_name || job.provider_id;
  }
  function renderTaskProviders(providers) {
    const select = $("task-provider");
    $("task-filter").hidden = !Array.isArray(providers);
    if (!Array.isArray(providers)) return;
    const entries = [["", "全部服务商"], ...providers.map((provider) => [
      provider.provider_id,
      provider.provider_name && provider.provider_name !== provider.provider_id
        ? `${provider.provider_name}（${provider.provider_id}）` : provider.provider_id
    ])];
    if (state.taskProvider && !entries.some(([id]) => id === state.taskProvider)) {
      entries.push([state.taskProvider, select.selectedOptions[0]?.textContent || state.taskProvider]);
    }
    if (entries.length !== select.options.length || entries.some(([id, title], i) =>
      select.options[i].value !== id || select.options[i].textContent !== title)) {
      fillOptions(select, entries, state.taskProvider);
    }
  }
  function renderTasks() {
    const focused = document.activeElement?.closest("[data-job-id]")?.dataset.jobId;
    const list = $("task-list");
    if (!state.jobs.length) list.replaceChildren(node("p", state.taskProvider ? "该服务商暂无保留任务。" : "尚无任务。选择服务商并创建一次扫描。", "empty"));
    else list.replaceChildren(...state.jobs.map((job) => {
      const button = node("button", undefined, "task");
      button.type = "button";
      button.dataset.jobId = job.scan_id;
      button.setAttribute("aria-pressed", String(job.scan_id === state.selectedId));
      const capability = capabilitySuffix(job.capability);
      button.setAttribute("aria-label", `${providerTitle(job)} 任务 ${job.scan_id.slice(0, 8)} ${labels[job.state] || job.state}${capability}`);
      const progress = job.operation === "models" ? `${job.progress.discovered} 项已发现` : `${job.progress.finished} 项已验证`;
      button.append(node("span", `${providerTitle(job)} / ${job.scan_id.slice(0, 8)}`, "task-title"), badge(job.state),
        node("span", dateText(job.created_at), "task-meta"), node("span", progress + capability, "task-meta"));
      button.addEventListener("click", () => selectJob(job.scan_id));
      return button;
    }));
    if (focused) [...list.querySelectorAll("button")].find((b) => b.dataset.jobId === focused)?.focus({preventScroll: true});
    $("task-count").textContent = `${state.total} 个保留任务`;
    $("page-info").textContent = `第 ${Math.floor(state.offset / pageSize) + 1} 页`;
    $("previous-page").disabled = state.offset === 0;
    $("next-page").disabled = state.offset + state.jobs.length >= state.total;
  }
  function renderProgress(job) {
    const block = $("progress-block");
    const track = $("progress-track");
    const bar = $("progress-bar");
    block.hidden = !job;
    if (!job) return;

    const {discovered, finished, planned, skipped = 0, cancelled = 0} = job.progress;
    const discoverOnly = job.operation === "models";
    const hasPlan = !discoverOnly && Number.isInteger(planned) && planned >= 0;
    const showBar = hasPlan && planned > 0;
    const pct = showBar ? Math.round(finished * 100 / planned) : 0;
    track.hidden = !showBar;
    if (showBar) {
      track.setAttribute("aria-valuemax", String(planned));
      track.setAttribute("aria-valuenow", String(finished));
    }
    bar.style.width = `${pct}%`;
    bar.classList.toggle("done", showBar && finished === planned && job.state === "completed");

    const counts = [`已发现 ${discovered} 项`];
    if (!discoverOnly && !hasPlan) counts.push(`已验证 ${finished} 项`);
    if (hasPlan && skipped) counts.push(`未执行 ${skipped} 项`);
    if (hasPlan && cancelled) counts.push(`已取消 ${cancelled} 项`);
    $("selection-progress").textContent = counts.join(" · ");

    const beforePlan = {
      queued: "排队等待执行",
      running: !discoverOnly && finished > 0 ? "正在验证模型" : "正在发现候选模型",
      cancelling: "正在取消",
      completed: discoverOnly ? "模型发现完成" : "验证任务已结束",
      failed: discoverOnly ? "模型发现失败" : "扫描未完成",
      cancelled: "任务已取消",
      interrupted: "任务中断"
    };
    $("progress-text").textContent = showBar ? `已验证 ${finished}/${planned} 项（${pct}%）` :
      hasPlan ? "没有纳入验证计划的模型" : beforePlan[job.state] || "";
  }
  function renderDetail() {
    const job = state.current;
    const partial = Boolean(job?.report_partial || state.report?.scan?.partial);
    $("selection-title").textContent = job ? `${providerTitle(job)} / ${job.scan_id}` : "选择任务查看状态与结果";
    $("selection-state").textContent = job ? `任务状态：${labels[job.state] || job.state}` : "尚未选择任务";
    const showCapability = job && job.operation !== "models";
    $("selection-capability").hidden = !showCapability;
    $("selection-capability").textContent = showCapability ? `探测能力 · ${capabilityLabel(job.capability)}` : "";
    renderProgress(job);
    $("selection-error").hidden = !job?.error;
    $("selection-error").textContent = job?.error || "";
    $("selection-partial").hidden = !partial;
    $("selection-source").hidden = !job?.source;
    $("selection-source-label").textContent = job?.source ? `来源对比 · ${job.source.endpoint_id}` : "";
    for (const side of ["baseline", "current"]) {
      $("open-source-" + side).title = job?.source?.[side + "_id"] || "";
    }
    $("cancel-scan").disabled = !job || !["queued", "running"].includes(job.state);
    $("download-json").disabled = !job?.report_available;
    $("download-xlsx").disabled = !job?.report_available || job?.operation === "models";
    $("download-passed").disabled = !job?.report_available || job?.operation === "models";
    renderComparisonButtons(job);
    if (!state.report) {
      $("results-empty").textContent = !job ? "选择一个任务。报告形成后可筛选结果；运行中可先查看上方进度。" :
        job.report_available ? "正在读取报告…" : ["completed", "cancelled", "failed", "interrupted"].includes(job.state) ? "任务已结束，无可下载报告。" : "任务进行中，报告尚未形成。";
    }
    renderResults();
  }
  function renderComparisonButtons(job) {
    for (const [side, other] of [["baseline", "current"], ["current", "baseline"]]) {
      const button = $("compare-" + side);
      button.disabled = state.comparison.retest.pending || !job?.report_available || !finishedTasks.has(job.state) ||
        job.scan_id === state.comparison[other]?.scan_id;
      button.setAttribute("aria-pressed", String(Boolean(job && job.scan_id === state.comparison[side]?.scan_id)));
    }
  }
  function comparisonValue(value, field) {
    if (value == null) return field === "probe.temperature" ? "沿用服务商默认" : "未记录";
    if (field === "probe.capability") return capabilityLabel(value);
    if (typeof value === "boolean") return value ? "是" : "否";
    return String(value);
  }
  function comparisonRow(row) {
    const tr = node("tr");
    const select = node("td");
    if (row.verification !== "unchanged" || row.catalog) {
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.setAttribute("aria-label", `复测 ${row.model}`);
      checkbox.checked = state.comparison.retest.models.has(row.model);
      checkbox.disabled = state.comparison.retest.pending;
      checkbox.addEventListener("change", () => setRetestModel(row.model, checkbox.checked));
      select.append(checkbox);
    }
    tr.append(select, node("td", row.model, "mono"));
    for (const [status, error] of [[row.baseline_status, row.baseline_error], [row.current_status, row.current_error]]) {
      const cell = node("td");
      cell.append(status ? badge(status) : node("span", "无验证记录", "small"));
      if (error && error !== "OK") cell.append(node("p", error, "small mono"));
      tr.append(cell);
    }
    const verification = node("td");
    if (row.verification === "not_tested") {
      const before = tested.has(row.baseline_status), now = tested.has(row.current_status);
      const capabilities = state.comparison.data?.capabilities;
      const mixed = capabilities && capabilities.baseline !== capabilities.current;
      const untested = mixed ? "探测能力不同，未配对" : before ? "本次未测" : now ? "基准未测" : "两次均未测";
      verification.append(node("span", untested, "badge warn"));
    } else if (verificationChanges[row.verification]) {
      const [label, tone] = verificationChanges[row.verification];
      verification.append(node("span", label, `badge ${tone}`));
    }
    const catalog = node("td", row.catalog === "added" ? "目录新增" : row.catalog === "removed" ? "目录移除" : "—");
    tr.append(verification, catalog);
    return tr;
  }
  function renderComparisonRows() {
    const rows = state.comparison.data?.rows || [];
    const search = $("compare-model").value.trim().toLowerCase();
    const filter = $("compare-filter").value;
    const matches = rows.filter((row) => row.model.toLowerCase().includes(search) &&
      (!filter || (filter === "catalog" ? Boolean(row.catalog) : row.verification === filter)));
    $("compare-count").textContent = `${matches.length} / ${rows.length} 项`;
    $("compare-empty").hidden = Boolean(matches.length);
    $("compare-table-wrap").hidden = !matches.length;
    const fragment = document.createDocumentFragment();
    for (const row of matches) fragment.append(comparisonRow(row));
    $("compare-rows").replaceChildren(fragment);
  }
  function renderComparison() {
    const pair = state.comparison;
    const {conditions, catalog, capabilities} = pair.data || {};
    $("compare-panel").hidden = !pair.baseline && !pair.current;
    if ($("compare-panel").hidden) return;
    for (const side of ["baseline", "current"]) {
      const job = pair[side];
      const value = capabilities?.[side] || job?.capability;
      const capability = value ? ` · ${capabilityLabel(value)}` : "";
      $("compare-" + side + "-label").textContent = job
        ? `${providerTitle(job)} · ${dateText(job.created_at)} · ${job.scan_id.slice(0, 8)}${capability}` : "从任务记录选择";
    }
    $("compare-endpoint-field").hidden = !pair.endpoints.length;
    const options = pair.endpoints.map((endpoint) => [endpoint.id, `${endpoint.id} · ${protocolLabel(endpoint.protocol)}`]);
    if (options.length > 1) options.unshift(["", "选择共同入口"]);
    fillOptions($("compare-endpoint"), options, pair.endpoint);
    $("compare-endpoint").disabled = options.length <= 1 || pair.retest.pending;
    $("clear-compare").disabled = pair.retest.pending;
    const message = pair.error || (pair.loading ? "正在读取对比…" :
      !pair.baseline || !pair.current ? "从任务记录选定两次任务后即可对比。" :
      !pair.endpoint ? "请选择要对比的共同入口。" : "");
    $("compare-message").textContent = message;
    $("compare-message").hidden = !message;
    $("compare-message").dataset.error = String(Boolean(pair.error));
    $("compare-result").hidden = !conditions || !catalog;
    if (!conditions || !catalog) return;
    $("compare-conditions").textContent = {
      matching: "已记录的探测条件一致", changed: "探测条件变化", unknown: "探测条件未能全部确认",
    }[conditions.status] || "探测条件未知";
    $("compare-credentials").textContent = {
      matching: "服务商配置修订一致", changed: "服务商配置修订不同", unknown: "凭据变化未知",
    }[conditions.credentials] || "";
    $("compare-catalog").textContent = `基准为${catalogCoverage[catalog.baseline]} · 本次为${catalogCoverage[catalog.current]}`;
    $("compare-catalog-status").textContent = catalog.comparable ? "目录可配对" : "目录新增与移除无法判定";
    $("compare-condition-changes").hidden = !conditions.changes.length;
    $("compare-condition-changes").replaceChildren(...conditions.changes.map(({field, baseline, current}) => node(
      "li", `${comparisonFields[field] || field}从 ${comparisonValue(baseline, field)} 变为 ${comparisonValue(current, field)}`,
    )));
    renderComparisonRows();
    renderRetest();
  }
  function renderRetest() {
    const {models, confirmation, changes, pending, message, error} = state.comparison.retest;
    $("retest-count").textContent = `已选 ${models.size} 项`;
    $("start-retest").disabled = pending || !models.size || Boolean(confirmation);
    $("clear-retest-models").disabled = pending || !models.size;
    $("retest-message").textContent = message;
    $("retest-message").hidden = !message;
    $("retest-message").dataset.error = String(error);
    $("retest-confirmation").hidden = !confirmation;
    $("confirm-retest").disabled = pending;
    $("retest-connection-changes").replaceChildren(...changes.map(({field, baseline, current}) =>
      node("li", `${comparisonFields[field] || field}从 ${comparisonValue(baseline, field)} 变为 ${comparisonValue(current, field)}`)));
  }
  function setRetestModel(model, checked) {
    const retest = state.comparison.retest;
    if (retest.pending) return;
    if (checked) retest.models.add(model);
    else retest.models.delete(model);
    Object.assign(retest, {requestId: "", confirmation: null, changes: [], message: "", error: false});
    renderRetest();
  }
  async function submitRetest(confirm = false) {
    const pair = state.comparison, retest = pair.retest;
    if (!pair.data?.endpoint_id || !retest.models.size || retest.pending ||
        confirm !== Boolean(retest.confirmation)) return;
    retest.requestId ||= crypto.randomUUID();
    const payload = {
      baseline_id: pair.baseline.scan_id, current_id: pair.current.scan_id, endpoint_id: pair.endpoint,
      models: [...retest.models], request_id: retest.requestId,
    };
    if (confirm) payload.confirmation = retest.confirmation;
    if (new TextEncoder().encode(JSON.stringify(payload)).length > 65536) {
      Object.assign(retest, {message: "所选模型超出请求体限制，请减少勾选项。", error: true});
      renderRetest();
      return;
    }
    const generation = pair.generation;
    Object.assign(retest, {pending: true, message: "正在提交复测…", error: false});
    renderComparisonButtons(state.current);
    renderComparison();
    try {
      const job = await api("/scans/retest", {method: "POST", body: payload, tag: "retest"});
      if (generation !== state.comparison.generation) return;
      pair.retest = retestState();
      selectCreatedJob(job);
      $("selection-title").focus();
      notice("复测任务已创建。");
      await refresh();
    } catch (error) {
      if (generation !== state.comparison.generation || error.name === "AbortError" || error.status === 401) return;
      const details = error.details;
      if (details?.code === "connection_changed" && typeof details.confirmation === "string") {
        Object.assign(retest, {
          confirmation: details.confirmation, changes: Array.isArray(details.changes) ? details.changes : [],
          message: "服务商连接配置已变化，请核对后确认。", error: false,
        });
      } else {
        const missingConnection = [
          "The current provider or endpoint is no longer available",
          "The selected endpoint is no longer available with this protocol",
        ].includes(details?.detail);
        const message = missingConnection ? "当前服务商或协议入口已不可用。" :
          error.status === 404 ? "找不到复测来源，该任务可能已被淘汰。" :
          error.message || "复测未提交。";
        Object.assign(retest, {message, error: true});
      }
    } finally {
      if (generation === state.comparison.generation) {
        retest.pending = false;
        renderComparisonButtons(state.current);
        renderComparison();
      }
    }
  }
  function invalidateComparison() {
    abortRequests("comparison");
    abortRequests("retest");
    state.comparison.generation += 1;
    Object.assign(state.comparison, {data: null, loading: false, error: "", retest: retestState()});
  }
  function clearComparison() {
    invalidateComparison();
    state.comparison = comparisonState(state.comparison.generation);
    $("compare-model").value = $("compare-filter").value = "";
    for (const id of ["compare-baseline-label", "compare-current-label", "compare-condition-changes", "compare-rows", "compare-endpoint"]) {
      $(id).replaceChildren();
    }
    renderComparisonButtons(state.current);
    renderComparison();
  }
  async function loadComparison() {
    const pair = state.comparison;
    if (!pair.baseline || !pair.current) return;
    invalidateComparison();
    const generation = pair.generation;
    pair.loading = true;
    renderComparison();
    const params = new URLSearchParams({baseline_id: pair.baseline.scan_id, current_id: pair.current.scan_id});
    if (pair.endpoint) params.set("endpoint_id", pair.endpoint);
    try {
      const result = await api(`/scans/compare?${params}`, {tag: "comparison"});
      if (generation !== state.comparison.generation) return;
      pair.data = result;
      pair.endpoints = result.endpoints;
      pair.endpoint = result.endpoint_id || "";
    } catch (error) {
      if (generation !== state.comparison.generation || error.name === "AbortError" || error.status === 401) return;
      pair.error = {
        404: "找不到对比任务，可能已被淘汰。请从任务记录重新选择。",
        409: "这两次任务尚无法对比，请选择已结束且有报告的任务。",
        422: "无法配对。请选择同一服务商且有共同协议入口的两次任务。",
      }[error.status] || error.message || "读取扫描对比失败。";
    } finally {
      if (generation === state.comparison.generation) {
        pair.loading = false;
        renderComparison();
      }
    }
  }
  function setComparisonTask(side) {
    const job = state.current, pair = state.comparison;
    const other = side === "baseline" ? "current" : "baseline";
    if (pair.retest.pending || !job?.report_available || !finishedTasks.has(job.state) ||
        [pair[side]?.scan_id, pair[other]?.scan_id].includes(job.scan_id)) return;
    invalidateComparison();
    Object.assign(pair, {[side]: job, endpoints: [], endpoint: ""});
    $("compare-model").value = $("compare-filter").value = "";
    renderComparisonButtons(job);
    renderComparison();
    if (pair.baseline && pair.current) {
      $("compare-title").focus();
      void loadComparison();
    }
  }
  function renderResults() {
    const all = state.report?.results || [];
    const search = $("search").value.toLowerCase();
    const matches = all.filter((row) => row.model.toLowerCase().includes(search) &&
      (!$("filter-protocol").value || row.protocol === $("filter-protocol").value) &&
      (!$("filter-status").value || row.status === $("filter-status").value) &&
      (!$("filter-error").value || row.error_category === $("filter-error").value));
    $("result-count").textContent = `${matches.length} / ${all.length} 条结果`;
    $("table-wrap").hidden = !matches.length;
    $("results-empty").hidden = Boolean(matches.length);
    if (state.report && !matches.length) $("results-empty").textContent = all.length ? "没有匹配当前筛选条件的结果。" : "此报告没有模型结果。请查看任务状态或下载 JSON 检查发现详情。";
    $("result-rows").replaceChildren(...matches.map((row) => {
      const tr = node("tr");
      const capability = capabilitySuffix(row.capability);
      const model = node("td"); model.append(node("p", row.model, "mono"), node("p", `${row.endpoint_id} · ${protocolLabel(row.protocol)}${capability}`, "small"));
      const status = node("td"); status.append(badge(row.status));
      const diagnostic = node("td"); diagnostic.append(node("p", row.error_category || "—", "mono"));
      if (row.diagnostic_code) diagnostic.append(node("p", row.diagnostic_code, "small mono"));
      const explanation = diagnosticCodeLabels[row.diagnostic_code] || diagnosticLabels[row.error_category];
      if (explanation) diagnostic.append(node("p", explanation, "small"));
      if (row.error_message) diagnostic.append(node("p", row.error_message, "small"));
      if (row.suggestion) diagnostic.append(node("p", row.suggestion, "small"));
      if (row.reason && !["probe_passed", "probe_failed"].includes(row.reason)) diagnostic.append(node("p", row.reason, "small"));
      tr.append(model, status, node("td", row.http_status ?? "—", "mono"), diagnostic,
        node("td", row.latency_ms == null ? "—" : `${Math.round(row.latency_ms)} ms`, "mono"), node("td", row.attempts, "mono"));
      return tr;
    }));
  }
  function clearReport() {
    state.report = null; state.reportId = null;
    fillOptions($("filter-error"), [["", "全部类别"]], "");
    $("search").value = "";
    $("filter-protocol").value = $("filter-status").value = "";
  }
  async function loadSelected() {
    const id = state.selectedId;
    if (!id || !state.connected) return;
    abortRequests("detail");
    const selection = state.selection;
    try {
      const job = await api(`/scans/${encodeURIComponent(id)}`, {tag: "detail"});
      if (selection !== state.selection || id !== state.selectedId) return;
      state.current = job;
      renderDetail();
      if (job.report_available && state.reportId !== id) {
        const report = await api(`/scans/${encodeURIComponent(id)}/report`, {tag: "detail"});
        if (selection !== state.selection || id !== state.selectedId) return;
        state.report = report; state.reportId = id;
        providerUI?.catalogue(report, job);
        const codes = [...new Set(report.results.map((r) => r.error_category).filter(Boolean))].sort();
        fillOptions($("filter-error"), [["", "全部类别"], ...codes.map((code) => [code, code])], "");
        renderDetail();
      }
    } catch (error) {
      if (selection !== state.selection) return;
      if (error.status === 404) {
        state.selectedId = null; state.current = null; clearReport(); renderTasks(); renderDetail();
        notice("该任务已不存在或已超出保留上限，请重新选择任务。");
      } else showError(error);
    }
  }
  function selectJob(id, job = state.jobs.find((item) => item.scan_id === id) || null) {
    notice("");
    state.selection += 1;
    abortRequests("detail"); abortRequests("download");
    state.selectedId = id;
    state.current = job;
    clearReport(); renderTasks(); renderDetail();
    void loadSelected();
  }
  function selectCreatedJob(job) {
    if (!state.taskProvider || state.taskProvider === job.provider_id) {
      state.offset = 0;
      state.taskQuery += 1;
    }
    selectJob(job.scan_id, job);
  }
  function changeTaskPage(offset) {
    state.offset = offset;
    state.taskQuery += 1;
    $("previous-page").disabled = $("next-page").disabled = true;
    abortRequests("poll");
    void refresh();
  }
  async function refresh() {
    if (!state.connected || state.refreshing === state.session) return;
    clearTimeout(state.timer);
    const session = state.session;
    const query = state.taskQuery;
    const params = new URLSearchParams({limit: pageSize, offset: state.offset});
    if (state.taskProvider) params.set("provider_id", state.taskProvider);
    state.refreshing = session;
    try {
      const [inventory, data] = await Promise.all([api("/providers", {tag: "poll"}), api(`/scans?${params}`, {tag: "poll"})]);
      if (session !== state.session || query !== state.taskQuery) return;
      managementEnabled = Boolean(inventory.management_enabled);
      applyInventory(inventory.providers);
      renderTaskProviders(data.providers);
      if (state.offset >= data.total && state.offset > 0) {
        changeTaskPage(Math.max(0, Math.floor((data.total - 1) / pageSize) * pageSize));
        return;
      }
      state.jobs = data.scans; state.total = data.total;
      renderTasks();
      $("connection-state").textContent = `已连接 · ${location.host}`;
      state.offline = false;
      await loadSelected();
    } catch (error) {
      if (session === state.session && query === state.taskQuery && state.connected && error.name !== "AbortError") {
        state.offline = true; $("connection-state").textContent = "连接异常 · 可立即刷新重试"; showError(error);
      }
    } finally {
      if (state.refreshing === session) state.refreshing = null;
      if (state.connected && session === state.session) state.timer = setTimeout(refresh, query !== state.taskQuery ? 0 : state.offline ? 5000 : 1500);
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("connect").disabled = true;
    state.session += 1;
    state.token = $("agent-token").value.trim();
    notice("");
    try {
      const inventory = await api("/providers");
      managementEnabled = Boolean(inventory.management_enabled);
      applyInventory(inventory.providers);
      state.connected = true;
      $("agent-token").value = "";
      $("login-panel").hidden = true; $("workspace").hidden = false; $("logout").hidden = false;
      await refresh();
    } catch (error) {
      if (state.token) disconnect("连接未完成，请检查本地 Agent 和会话 Token。");
      showError(error);
    } finally { $("connect").disabled = false; }
  });
  $("logout").addEventListener("click", () => disconnect("已退出连接。后台任务不会被取消，重新连接后可查看。"));
  $("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
  });
  $("provider").addEventListener("change", updateEndpoints);
  $("endpoint").addEventListener("change", updateLimits);
  $("profile").addEventListener("change", updateLimits);
  for (const id of ["scan-mode", "model-mode"]) $(id).addEventListener("change", updateScanMode);
  $("scan-form").addEventListener("input", updatePreviews);
  $("refresh").addEventListener("click", () => void refresh());
  $("task-provider").addEventListener("change", () => {
    state.taskProvider = $("task-provider").value;
    if (state.taskProvider && state.current?.provider_id !== state.taskProvider) selectJob(null);
    changeTaskPage(0);
  });
  $("previous-page").addEventListener("click", () => changeTaskPage(Math.max(0, state.offset - pageSize)));
  $("next-page").addEventListener("click", () => changeTaskPage(state.offset + pageSize));
  for (const id of ["search", "filter-protocol", "filter-status", "filter-error"]) $(id).addEventListener(id === "search" ? "input" : "change", renderResults);

  async function createTask(payload, message, slot) {
    if (state.creating || !state.connected) return;
    if (slot) {
      const preview = state.previews[slot];
      if (!preview?.data || preview.data.effective_max_requests <= 0 ||
          JSON.stringify(payload) !== JSON.stringify(preview.payload)) return;
    }
    state.creating = true; updatePreviews();
    const session = state.session;
    try {
      const job = await api("/scans", {method: "POST", body: payload, tag: "mutation"});
      if (session !== state.session) return;
      selectCreatedJob(job);
      notice(message);
      await refresh();
    } catch (error) { showError(error); }
    finally { if (session === state.session) { state.creating = false; updatePreviews(); } }
  }
  $("scan-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const {payload} = scanDraft();
    if (payload) void createTask(payload, payload.mode === "quick" ? "指定模型验证已创建。" : "批量验收任务已创建。", "scan");
  });
  $("cancel-scan").addEventListener("click", async () => {
    const id = state.selectedId;
    if (!id) return;
    $("cancel-scan").disabled = true;
    try {
      const job = await api(`/scans/${encodeURIComponent(id)}/cancel`, {method: "POST", tag: "mutation"});
      if (id === state.selectedId) { state.current = job; renderDetail(); }
      notice("已提交取消请求；已完成的模型结果会保留。");
      await refresh();
    } catch (error) { showError(error); }
  });
  function exportFilename(job, format, scope = "all") {
    const partial = job.report_partial || state.report?.scan?.partial;
    if (format !== "xlsx") return `scan-${job.scan_id}${partial ? "-partial" : ""}.${format}`;
    const raw = (job.provider_name || job.provider_id || "scan").trim();
    const safe = (raw.replace(/[\\/:*?"<>|]/g, "_").slice(0, 60).replace(/[. ]+$/, "")) || "scan";
    const date = new Date(job.finished_at || job.created_at);
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = Number.isNaN(date.getTime())
      ? "" : `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
    return `${safe}-${stamp}${partial ? "-部分" : ""}${scope === "passed" ? "-仅通过" : ""}.xlsx`;
  }
  async function download(format, scope = "all") {
    const id = state.selectedId;
    if (!id || !state.current?.report_available) return;
    try {
      const suffix = format === "xlsx" && scope !== "all" ? `&scope=${scope}` : "";
      const data = await api(`/scans/${encodeURIComponent(id)}/report?format=${format}${suffix}`, {tag: "download", blob: true});
      if (id !== state.selectedId) return;
      const url = URL.createObjectURL(data); state.downloads.add(url);
      const anchor = node("a"); anchor.href = url;
      anchor.download = exportFilename(state.current, format, scope);
      document.body.append(anchor); anchor.click(); anchor.remove();
      setTimeout(() => { URL.revokeObjectURL(url); state.downloads.delete(url); }, 1000);
    } catch (error) { showError(error); }
  }
  $("download-json").addEventListener("click", () => void download("json"));
  $("download-xlsx").addEventListener("click", () => void download("xlsx"));
  $("download-passed").addEventListener("click", () => void download("xlsx", "passed"));
  $("compare-baseline").addEventListener("click", () => setComparisonTask("baseline"));
  $("compare-current").addEventListener("click", () => setComparisonTask("current"));
  $("clear-compare").addEventListener("click", () => {
    clearComparison();
    $("compare-baseline").focus();
  });
  $("compare-endpoint").addEventListener("change", (event) => {
    if (state.comparison.retest.pending) return;
    state.comparison.endpoint = event.currentTarget.value;
    void loadComparison();
  });
  $("compare-model").addEventListener("input", renderComparisonRows);
  $("compare-filter").addEventListener("change", renderComparisonRows);
  $("start-retest").addEventListener("click", () => void submitRetest());
  $("confirm-retest").addEventListener("click", () => void submitRetest(true));
  $("clear-retest-models").addEventListener("click", () => {
    if (state.comparison.retest.pending) return;
    state.comparison.retest = retestState();
    renderComparison();
  });
  for (const side of ["baseline", "current"]) $("open-source-" + side).addEventListener("click", () => {
    const id = state.current?.source?.[side + "_id"];
    if (id) selectJob(id);
  });
  window.addEventListener("pagehide", () => disconnect(""));
  providerUI = window.createProviderUI({
    api, notice, showError, protocolLabel, getProvider: selectedProvider,
    onSaved: async (id) => {
      const inventory = await api('/providers');
      managementEnabled = Boolean(inventory.management_enabled);
      applyInventory(inventory.providers);
      if (id) $("provider").value = id;
      updateEndpoints();
      await refresh();
    },
    onCatalogChange: updateCatalogPreviews,
    onTask: (payload, slot) => createTask(taskSettings(payload),
      payload.operation === "models" ? "模型获取任务已创建，完成后可勾选并测试。" : "批量验收任务已创建。", slot),
  });
  updateScanMode();
  fetch("/health", {credentials: "omit", cache: "no-store"}).then((r) => r.ok ? r.json() : null).then((data) => {
    if (data?.version) $("version").textContent = `Model Scout ${data.version}`;
  }).catch(() => {});
})();
