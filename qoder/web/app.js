"use strict";

const state = { experiments: [], currentId: null, scopeConfigured: false };

const el = (id) => document.getElementById(id);

function setText(id, value) {
  el(id).textContent = value == null ? "" : String(value);
}

function setStatus(text, kind) {
  const node = el("service-status");
  node.textContent = text;
  node.className = `status ${kind}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = { ok: false, state: "api_error", error: { kind: "invalid_json", message: `响应不是 JSON（HTTP ${response.status}）` } };
  }
  return { response, payload };
}

function describeFailure(payload) {
  const detail = (payload && payload.error) || {};
  if (payload.state === "service_unavailable") {
    return { kind: "error", text: `PowerContext 服务不可用：${detail.message || "无法连接"}` };
  }
  const code = detail.code ? `（${detail.code}）` : "";
  return { kind: "error", text: `PowerContext 接口错误：${detail.message || "未知错误"}${code}` };
}

function renderExperiments() {
  const list = el("experiment-list");
  list.textContent = "";
  if (!state.experiments.length) {
    const empty = document.createElement("li");
    empty.textContent = "还没有实验。创建第一个实验即可开始记录。";
    empty.className = "empty";
    list.appendChild(empty);
    return;
  }
  state.experiments.forEach((item) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = item.id === state.currentId ? "select selected" : "select";
    button.textContent = item.title;
    button.dataset.id = item.id;
    const scope = document.createElement("span");
    scope.className = "scope";
    scope.textContent = item.scope_id;
    li.append(button, scope);
    list.appendChild(li);
  });
}

function selectExperiment(id) {
  state.currentId = id;
  const experiment = state.experiments.find((item) => item.id === id);
  if (experiment) {
    setText(
      "current-experiment",
      `当前实验：${experiment.title}｜Scope ${experiment.scope_id}｜来源 ${experiment.scope_origin}`,
    );
  }
  renderExperiments();
}

async function refreshExperiments() {
  let payload;
  try {
    ({ payload } = await api("/api/experiments"));
  } catch (error) {
    setStatus(`后端不可用：${error.message}`, "error");
    return;
  }
  if (!payload.ok) {
    setStatus(describeFailure(payload).text, "error");
    return;
  }
  state.experiments = payload.experiments || [];
  state.scopeConfigured = Boolean(payload.config && payload.config.powercontext_scope_configured);
  el("use-configured-scope").disabled = !state.scopeConfigured;
  setText("scope-hint", state.scopeConfigured
    ? "已配置 POWERCONTEXT_SCOPE，可作为默认实验的 Scope 复用；其他实验始终创建独立 Scope。"
    : "未配置 POWERCONTEXT_SCOPE：每个实验都会创建并持久保存自己的 Scope 映射。");
  renderExperiments();
  if (!state.currentId && state.experiments.length) {
    selectExperiment(state.experiments[state.experiments.length - 1].id);
  }
}

async function checkHealth() {
  try {
    const { payload } = await api("/api/health");
    if (payload.ok) {
      const service = payload.powercontext || {};
      setStatus(`PowerContext ${service.status || "ready"}`, "ok");
    } else {
      const info = describeFailure(payload);
      setStatus(info.text, "error");
    }
    return payload;
  } catch (error) {
    setStatus(`后端不可用：${error.message}`, "error");
    return null;
  }
}

function requireExperiment(target) {
  if (!state.currentId) {
    setText(target, "请先创建或选择一个实验。");
    el(target).className = "result error";
    return false;
  }
  return true;
}

function renderRecall(payload) {
  el("recall-card").hidden = true;
  el("entries-heading").hidden = true;
  el("entry-list").textContent = "";

  if (!payload.ok) {
    const info = describeFailure(payload);
    setText("recall-state", info.text);
    el("recall-state").className = "result error";
    return;
  }
  if (payload.state === "no_memory") {
    setText("recall-state", `无记忆：${payload.message}（Scope ${payload.scope_id}，status ${payload.prepared_status}）`);
    el("recall-state").className = "result warning";
    return;
  }

  const latest = payload.latest || {};
  const parsed = latest.parsed || {};
  const sources = payload.recall_sources || {};
  el("recall-card").hidden = false;
  setText(
    "recall-state",
    `已从 PowerContext 召回 ${payload.entries.length} 条进展记忆（status ${payload.prepared_status}${payload.truncated ? "，内容已截断" : ""}）。`,
  );
  el("recall-state").className = "result success";
  if (payload.warning) {
    setText("recall-warning", payload.warning);
    el("recall-warning").hidden = false;
  } else {
    el("recall-warning").hidden = true;
    setText("recall-warning", "");
  }
  setText("recalled-title", parsed.title || "（召回文本不符合进展格式，见下方原文）");
  setText("recalled-progress", parsed.progress || "（未解析出进展字段，见下方原文）");
  setText("recalled-next", parsed.next_step || "（未解析出下一步，见下方原文）");
  setText("recalled-saved-at", parsed.ordinal ? `第 ${parsed.ordinal} 次保存 · ${parsed.saved_at}` : parsed.saved_at || "");
  setText(
    "recalled-codec",
    parsed.decodable === false
      ? `编码 ${parsed.codec || "unknown"}：无法解析为结构化字段，以下为 PowerContext 原文`
      : `编码 ${parsed.codec || "unknown"}`,
  );
  setText("recalled-scope", payload.scope_id);
  setText("recalled-entry", latest.entry_id || "");
  setText("recalled-version", latest.entry_version_id || "");
  setText("recalled-query", payload.query);
  setText(
    "recalled-source",
    `最新条目来自 ${sources.latest_from === "memory_entries_list" ? "Scope 记忆清单补全（/v1/memory/entries/list）" : "相关度召回（/v1/context/prepare）"}；` +
      `候选 ${sources.prepare_candidate_count ?? 0} 条 / 清单 ${sources.inventory_entry_count ?? 0} 条` +
      (sources.undecodable_entry_count ? `；其中 ${sources.undecodable_entry_count} 条无法解析为结构化字段` : ""),
  );
  const preparedShowsLatest = sources.latest_from !== "memory_entries_list";
  setText(
    "recalled-content",
    preparedShowsLatest
      ? payload.content || latest.text || ""
      : `相关度召回候选未包含最新保存，以下条目直接来自该 Scope 的记忆清单：\n${latest.text}\n\n` +
          `—— /v1/context/prepare 返回内容 ——\n${payload.content || "(status " + payload.prepared_status + ", 无候选)"}`,
  );

  el("entries-heading").hidden = false;
  [...payload.entries].reverse().forEach((item) => {
    const li = document.createElement("li");
    const heading = document.createElement("strong");
    const ordinal = (item.parsed || {}).ordinal;
    heading.textContent = `${item.entry_id} / ${item.entry_version_id}${ordinal ? ` · 第 ${ordinal} 次保存` : ""}`;
    const body = document.createElement("span");
    body.textContent = item.text;
    li.append(heading, body);
    el("entry-list").appendChild(li);
  });
}

function wireHandlers() {
  el("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = el("experiment-title").value.trim();
    if (!title) return;
    setStatus("正在创建实验……", "pending");
    const { payload } = await api("/api/experiments", {
      method: "POST",
      body: JSON.stringify({ title, use_configured_scope: el("use-configured-scope").checked }),
    });
    if (!payload.ok) {
      setStatus(describeFailure(payload).text, "error");
      return;
    }
    await refreshExperiments();
    selectExperiment(payload.experiment.id);
    setStatus(`实验已绑定 Scope（${payload.scope_origin}）`, "ok");
    el("experiment-title").value = "";
  });

  el("experiment-list").addEventListener("click", (event) => {
    const button = event.target.closest("button.select");
    if (button) selectExperiment(button.dataset.id);
  });

  el("save-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requireExperiment("save-result")) return;
    el("save-button").disabled = true;
    setText("save-result", "正在写入 PowerContext……");
    el("save-result").className = "result pending";
    const { payload } = await api(`/api/experiments/${encodeURIComponent(state.currentId)}/save`, {
      method: "POST",
      body: JSON.stringify({ progress: el("progress").value, next_step: el("next-step").value }),
    });
    el("save-button").disabled = false;
    if (!payload.ok) {
      setText("save-result", describeFailure(payload).text);
      el("save-result").className = "result error";
      return;
    }
    const citation = (payload.entry || {}).citation || {};
    const recorded = payload.experiment || {};
    const entryId = citation.entry_id || recorded.entry_id || "无（服务未返回 entry）";
    const versionId = citation.entry_version_id || recorded.entry_version_id || "无（服务未返回 entry）";
    const source = citation.entry_id ? "/v1/memory/remember 响应" : "本地记录的上一份已确认 ID";
    setText(
      "save-result",
      `${
        payload.deduplicated
          ? `已写入 Scope ${payload.scope_id}：相同内容的记忆已存在（幂等写入，未新增条目）。`
          : `已写入 Scope ${payload.scope_id}，第 ${payload.ordinal} 次保存。`
      }entry ${entryId} / version ${versionId}（来源：${source}）`,
    );
    el("save-result").className = "result success";
    await refreshExperiments();
  });

  el("recall-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requireExperiment("recall-state")) return;
    el("recall-button").disabled = true;
    setText("recall-state", "正在召回……");
    el("recall-state").className = "result pending";
    const { payload } = await api(`/api/experiments/${encodeURIComponent(state.currentId)}/recall`, {
      method: "POST",
      body: JSON.stringify({ question: el("question").value }),
    });
    el("recall-button").disabled = false;
    renderRecall(payload);
  });
}

wireHandlers();
refreshExperiments().then(checkHealth);
