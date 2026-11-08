// Persona_ize 데모 화면. 빌드 없는 바닐라 JS — 백엔드 JSON API를 같은 출처에서 호출한다.
// 인증: 세션 쿠키(HttpOnly) + 쓰기 요청의 X-CSRF-Token. 토큰은 로그인 응답·/v1/auth/me 에서 받는다.

const $ = (sel) => document.querySelector(sel);
const state = { me: null, csrf: null, contacts: [], contact: null, persona: null, conversations: [], conv: null, rewrite: null };

const REL_LABEL = { superior: "상사", colleague: "동료", subordinate: "후배", friend: "친구", family: "가족", partner: "연인", acquaintance: "지인" };

function flash(msg, isError = false) {
  const el = $("#flash");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.toggle("hidden", !msg);
}

async function api(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  if (state.csrf && method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  const res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) {
    if (res.status === 401 && state.me) { state.me = null; renderAuth(); }
    const msg = data?.message || (Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join(", ") : data?.detail?.message) || `${res.status} 오류`;
    throw new Error(msg);
  }
  return data;
}

// ---------- 인증 ----------
async function loadMe() {
  try {
    const me = await api("GET", "/v1/auth/me");
    state.me = me; state.csrf = me.csrf_token;
  } catch { state.me = null; state.csrf = null; }
  renderAuth();
  if (state.me) { await loadContacts(); await loadUsage(); }
}

function renderAuth() {
  $("#auth-form").classList.toggle("hidden", !!state.me);
  $("#auth-me").classList.toggle("hidden", !state.me);
  $("#app").classList.toggle("hidden", !state.me);
  if (state.me) $("#me-name").textContent = state.me.username;
}

async function loadUsage() {
  try {
    const u = await api("GET", "/v1/usage");
    $("#usage").textContent = `오늘 ${u.rewrites}/${u.rewrite_limit}회 · ${u.input_tokens + u.output_tokens} 토큰`;
  } catch { /* 표시용 */ }
}

$("#auth-form").addEventListener("submit", (e) => { e.preventDefault(); auth("login"); });
$("#auth-form button[data-action=register]").addEventListener("click", () => auth("register"));
$("#pw-change").addEventListener("click", async () => {
  const current = prompt("현재 비밀번호");
  if (!current) return;
  const next = prompt("새 비밀번호 (8자 이상)");
  if (!next) return;
  try {
    const r = await api("POST", "/v1/auth/password", { current_password: current, new_password: next });
    flash(`비밀번호를 바꿨습니다. 다른 기기의 세션 ${r.revoked_sessions}개를 로그아웃했습니다.`);
  } catch (err) { flash(err.message, true); }
});
$("#logout").addEventListener("click", async () => { await api("POST", "/v1/auth/logout"); state.me = null; state.csrf = null; renderAuth(); });

async function auth(kind) {
  try {
    const r = await api("POST", `/v1/auth/${kind}`, { username: $("#auth-user").value.trim(), password: $("#auth-pass").value });
    state.csrf = r.csrf_token; state.me = { user_id: r.user_id, username: r.username };
    $("#auth-pass").value = "";
    flash("");
    renderAuth(); await loadContacts(); await loadUsage();
  } catch (err) { flash(err.message, true); }
}

// ---------- 상대 ----------
async function loadContacts() {
  state.contacts = await api("GET", "/v1/contacts");
  const ul = $("#contact-list");
  ul.innerHTML = "";
  $("#contact-empty").classList.toggle("hidden", state.contacts.length > 0);
  for (const c of state.contacts) {
    const li = document.createElement("li");
    li.className = state.contact?.id === c.id ? "selected" : "";
    li.innerHTML = `<span>${esc(c.alias)} <span class="tag">${REL_LABEL[c.relationship_type] || c.relationship_type}</span></span><span class="muted">${c.persona_version ? `v${c.persona_version}` : "페르소나 없음"}</span>`;
    li.addEventListener("click", () => selectContact(c));
    ul.appendChild(li);
  }
}

$("#contact-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const c = await api("POST", "/v1/contacts", { alias: $("#contact-alias").value, relationship_type: $("#contact-rel").value });
    $("#contact-alias").value = "";
    await loadContacts(); await selectContact(c);
  } catch (err) { flash(err.message, true); }
});

async function selectContact(c) {
  state.contact = c; state.conv = null; state.rewrite = null;
  $("#persona-box").classList.remove("hidden");
  $("#rewrite-form").classList.remove("hidden");
  $("#rewrite-hint").classList.add("hidden");
  $("#history-box").classList.remove("hidden");
  $("#result").classList.add("hidden");
  await loadContacts();
  await loadPersona();
  await loadConversations();
  await loadHistory();
}

// ---------- 첨삭 이력 ----------
async function loadHistory() {
  if (!state.contact) return;
  const rows = await api("GET", `/v1/rewrites?contact_id=${state.contact.id}&limit=10`);
  const ul = $("#history-list");
  ul.innerHTML = "";
  $("#history-empty").classList.toggle("hidden", rows.length > 0);
  $("#history-count").textContent = rows.length ? `${rows.length}건` : "";
  for (const rec of rows) {
    const li = document.createElement("li");
    const r = rec.result;
    const when = new Date(rec.created_at).toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    const cond = (r.retrieval_version === "none") ? "A/B" : (r.retrieval_version.includes("graph") ? "D" : "C");
    li.innerHTML = `<span class="hist"><span class="muted"><span class="when">${when}</span><span class="tag">${cond}</span> <span class="${r.status === "recommended" ? "status-ok" : "status-fallback"}">${r.status === "recommended" ? "추천" : "원문 유지"}</span></span><span class="text">${esc(r.result_text)}</span></span>`;
    li.className = state.rewrite?.id === rec.id ? "selected" : "";
    li.addEventListener("click", () => { state.rewrite = rec; renderResult(rec); loadHistory(); $("#result").scrollIntoView({ behavior: "smooth", block: "nearest" }); });
    ul.appendChild(li);
  }
}

// ---------- 페르소나 ----------
const personaForm = $("#persona-form");
for (const name of ["formality", "friendliness", "directness"]) {
  personaForm.elements[name].addEventListener("input", (e) => { $(`#o-${name}`).textContent = e.target.value; });
}

async function loadPersona() {
  try {
    state.persona = await api("GET", `/v1/contacts/${state.contact.id}/persona`);
  } catch { state.persona = null; }
  const s = state.persona?.settings || {};
  const f = personaForm.elements;
  for (const name of ["formality", "friendliness", "directness"]) { f[name].value = s[name] ?? 3; $(`#o-${name}`).textContent = f[name].value; }
  f.length_pref.value = s.length || "same"; // form.elements.length 는 컨트롤 개수라 이름을 피한다
  f.emoji.value = s.emoji || "none";
  f.address_term.value = s.address_term || ""; f.notes.value = s.notes || "";
  f.preferred_phrases.value = (s.preferred_phrases || []).join(", ");
  f.forbidden_phrases.value = (s.forbidden_phrases || []).join(", ");
  $("#persona-version").textContent = state.persona ? `v${state.persona.version}` : "(아직 없음 — 저장하면 v1)";
}

personaForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const saveBtn = personaForm.querySelector("button[type=submit]");
  saveBtn.disabled = true;
  const f = personaForm.elements;
  const list = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const settings = {
    formality: +f.formality.value, friendliness: +f.friendliness.value, directness: +f.directness.value,
    length: f.length_pref.value, emoji: f.emoji.value,
    address_term: f.address_term.value.trim() || null, notes: f.notes.value.trim() || null,
    preferred_phrases: list(f.preferred_phrases.value), forbidden_phrases: list(f.forbidden_phrases.value),
  };
  try {
    await api("PUT", `/v1/contacts/${state.contact.id}/persona`, { settings, expected_version: state.persona?.version ?? 0 });
    flash("페르소나를 저장했습니다.");
    await loadPersona(); await loadContacts();
  } catch (err) { flash(err.message, true); if (/충돌/.test(err.message)) await loadPersona(); }
  finally { saveBtn.disabled = false; }
});

// ---------- 과거 대화 ----------
async function loadConversations() {
  state.conversations = await api("GET", `/v1/conversations?contact_id=${state.contact.id}`);
  const ul = $("#conv-list");
  ul.innerHTML = "";
  for (const c of state.conversations) {
    const li = document.createElement("li");
    li.className = state.conv?.id === c.id ? "selected" : "";
    li.innerHTML = `<span>${esc(c.title)} <span class="tag">${esc(c.source_key)}</span></span>`;
    const del = document.createElement("button");
    del.className = "danger"; del.textContent = "삭제";
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`'${c.title}'의 원문·검색 데이터·그래프 기록을 모두 지웁니다. 계속할까요?`)) return;
      try {
        const d = await api("DELETE", `/v1/conversations/${c.id}`);
        flash(`삭제: 메시지 ${d.messages}건, 묶음 ${d.episodes}개, 그래프 ${d.graph_episodes}개${d.graph_error ? ` (그래프 오류: ${d.graph_error})` : ""}`);
        if (state.conv?.id === c.id) state.conv = null;
        await loadConversations();
      } catch (err) { flash(err.message, true); }
    });
    li.appendChild(del);
    li.addEventListener("click", () => { state.conv = c; loadConversations(); });
    ul.appendChild(li);
  }
  $("#conv-empty").classList.toggle("hidden", state.conversations.length > 0);
  $("#import-form").classList.toggle("hidden", !state.conv);
  $("#msg-box").classList.toggle("hidden", !state.conv);
  if (state.conv) {
    $("#import-me-name").value = state.conv.me_name || "";
    await loadMessages();
  }
}

async function loadMessages() {
  const msgs = await api("GET", `/v1/conversations/${state.conv.id}/messages`);
  const ul = $("#msg-list");
  ul.innerHTML = "";
  $("#msg-count").textContent = msgs.length ? `${msgs.length}건` : "(없음)";
  for (const m of msgs.slice(-50)) {
    const li = document.createElement("li");
    li.className = m.speaker_role;
    const when = new Date(m.sent_at).toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    li.innerHTML = `<span class="when">${when}</span>${m.speaker_role === "me" ? "나" : esc(state.contact.alias)}: ${esc(m.text)}`;
    ul.appendChild(li);
  }
}

$("#conv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const c = await api("POST", "/v1/conversations", {
      contact_id: state.contact.id, title: $("#conv-title").value.trim() || null, me_name: $("#conv-me").value.trim() || null,
    });
    $("#conv-title").value = "";
    state.conv = c; await loadConversations();
  } catch (err) { flash(err.message, true); }
});

function parseImport(text, meName, contactName) {
  const trimmed = text.trim();
  if (trimmed.startsWith("[")) return { messages: JSON.parse(trimmed), speaker_map: meName ? { [meName]: "me", [contactName]: "contact" } : {} };
  const messages = []; const names = new Set();
  trimmed.split(/\r?\n/).forEach((line, i) => {
    if (!line.trim()) return;
    const parts = line.split("|").map((x) => x.trim());
    if (parts.length < 3) throw new Error(`${i + 1}번째 줄: '날짜 시각 | 화자 | 내용' 형식이 아닙니다.`);
    const when = parts[0].replace(" ", "T");
    const sent_at = /[+Z]/.test(when.slice(10)) ? when : `${when}:00+09:00`.replace(/:00:00\+/, ":00+");
    names.add(parts[1]);
    messages.push({ id: `m${String(i + 1).padStart(2, "0")}`, speaker: parts[1], sent_at, text: parts.slice(2).join(" | ") });
  });
  const speaker_map = {};
  for (const n of names) {
    if (n === "me" || n === "contact") continue;
    speaker_map[n] = n === meName ? "me" : "contact";
  }
  return { messages, speaker_map };
}

$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("#import-status");
  try {
    const meName = $("#import-me-name").value.trim();
    const payload = parseImport($("#import-text").value, meName, state.contact.alias);
    status.textContent = "가져오는 중…";
    let job = await api("POST", `/v1/conversations/${state.conv.id}/imports`, payload);
    status.textContent = `메시지 ${job.imported}건 추가, ${job.duplicates}건 중복, 문체 묶음 ${job.episodes}개. 그래프: ${job.graph_status}`;
    while (job.graph_status === "pending" || job.graph_status === "running") {
      await new Promise((r) => setTimeout(r, 4000));
      job = await api("GET", `/v1/imports/${job.id}`);
      status.textContent = `메시지 ${job.imported}건 추가 · 그래프 수집 ${job.graph_status} (${job.graph_episodes}개)`;
    }
    if (job.graph_status === "failed") status.textContent += ` — 그래프 오류: ${job.graph_error}`;
    if (job.graph_status === "completed") status.textContent += " · 완료";
    if (job.graph_status === "skipped") status.textContent += " (그래프는 NEO4J_URI가 없어 건너뜀)";
    $("#import-text").value = "";
    await loadMessages();
  } catch (err) { status.textContent = ""; flash(err.message, true); }
});

// ---------- 첨삭 ----------
$("#rewrite-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const btn = $("#rewrite-submit");
  btn.disabled = true; btn.textContent = "생성 중…";
  try {
    const headers = { "Idempotency-Key": crypto.randomUUID() };
    const res = await fetch("/v1/rewrites", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf, ...headers },
      body: JSON.stringify({ contact_id: state.contact.id, draft_text: f.draft_text.value, situation: f.situation.value, condition: f.condition.value }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || data.detail?.message || `${res.status} 오류`);
    state.rewrite = data; renderResult(data); await loadUsage(); await loadHistory();
  } catch (err) { flash(err.message, true); }
  finally { btn.disabled = false; btn.textContent = "추천 받기"; }
});

function renderResult(rec) {
  const r = rec.result;
  $("#result").classList.remove("hidden");
  const ok = r.status === "recommended";
  const st = $("#result-status");
  st.textContent = ok ? "추천" : "추천 실패 — 원문 유지";
  st.className = ok ? "status-ok" : "status-fallback";
  $("#result-meta").textContent = `${r.model_version} · ${r.retrieval_version} · ${r.attempts}회 시도 · ${r.latency_ms}ms`;
  $("#result-text").value = r.result_text;
  fill("#result-reasons", r.change_reasons, "(없음)");
  fill("#result-warnings", r.warnings, "(없음)");
  fill("#result-preserved", r.preserved_items.map((p) => `${p.kind}: ${p.text}`), "(추출된 항목 없음)");
  $("#result-validation").textContent = r.validation.passed ? "검증 통과" : "검증 실패: " + r.validation.violations.map((v) => v.detail).join(" / ");
  fill("#result-evidence", r.evidence_ids, "(참고한 과거 표현 없음)");
}

function fill(sel, items, empty) {
  const ul = $(sel);
  ul.innerHTML = "";
  for (const it of items.length ? items : [empty]) { const li = document.createElement("li"); li.textContent = it; ul.appendChild(li); }
}

async function feedback(accepted, edited, reason) {
  if (!state.rewrite) return;
  try {
    await api("POST", `/v1/rewrites/${state.rewrite.id}/feedback`, { accepted, edited_text: edited || null, reason: reason || null });
    flash(accepted ? "피드백을 저장했습니다. 메시지는 직접 붙여 넣어 보내세요 (자동 전송 없음)." : "거절을 기록했습니다.");
  } catch (err) { flash(err.message, true); }
}
async function copyText(text) {
  // 클립보드는 포커스가 없거나 권한이 없으면 실패한다 — 피드백 저장은 그래도 진행한다.
  try { await navigator.clipboard?.writeText(text); return true; } catch { return false; }
}
$("#fb-accept").addEventListener("click", async () => {
  const copied = await copyText($("#result-text").value);
  await feedback(true, null, null);
  if (!copied) flash("피드백은 저장했지만 클립보드 복사는 실패했습니다. 텍스트를 직접 복사하세요.");
});
$("#fb-edit").addEventListener("click", async () => {
  const edited = $("#result-text").value;
  await copyText(edited);
  await feedback(true, edited !== state.rewrite.result.result_text ? edited : null, null);
});
$("#fb-reject").addEventListener("click", async () => { const reason = prompt("거절 이유 (선택)") || null; await feedback(false, null, reason); });

function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

loadMe();
