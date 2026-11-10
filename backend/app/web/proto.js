// Persona_ize 시연 프로토타입 (와이어프레임 v2: M1~M4, P1).
// 메신저 화면은 흉내이고, 첨삭만 실제 API(/v1/rewrites)를 부른다. 인증은 app.js와 같다(세션 쿠키 + X-CSRF-Token).

const $ = (sel) => document.querySelector(sel);
const state = {
  me: null, csrf: null, contacts: [], contact: null,
  mode: "mobile", // mobile | pc
  stage: "idle", // idle | pick | loading | result | fallback | error
  editing: false, rec: null, error: "", requestSeq: 0,
};

const REL_LABEL = { superior: "상사", colleague: "동료", subordinate: "후배", friend: "친구", family: "가족", partner: "연인", acquaintance: "지인" };
// 시연 대본(docs/demo-script.md)의 원문. 상대 이름에 들어 있으면 초안으로 채운다.
const DEMO_DRAFTS = {
  박지현: "이번 주 금요일은 어렵고 다음 주 수요일까지는 가능합니다.",
  이서연: "어제는 제가 좀 예민했던 것 같아요. 미안합니다.",
  최유진: "다음 달 둘째 주 토요일 저녁 어때?",
  김준호: "토요일 오전에 차 좀 빌려줄 수 있어?",
};
const LAST_CONTACT_KEY = "pi_proto_last_contact";

function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

async function api(method, path, body, extraHeaders = {}) {
  const headers = { "Content-Type": "application/json", ...extraHeaders };
  if (state.csrf && method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  const res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) {
    const msg = data?.message || (Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join(", ") : data?.detail?.message) || `${res.status} 오류`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

function remember(id) { try { localStorage.setItem(LAST_CONTACT_KEY, id); } catch { /* 저장 못 해도 동작한다 */ } }
function recall() { try { return localStorage.getItem(LAST_CONTACT_KEY); } catch { return null; } }

// ---------- 인증·초기 데이터 ----------
async function boot() {
  try {
    const me = await api("GET", "/v1/auth/me");
    state.me = me; state.csrf = me.csrf_token;
  } catch { state.me = null; }
  $("#login").classList.toggle("hidden", !!state.me);
  $("#app").classList.toggle("hidden", !state.me);
  $("#me").textContent = state.me ? `${state.me.username} 계정` : "";
  if (!state.me) return;
  state.contacts = await api("GET", "/v1/contacts");
  const sel = $("#contact");
  sel.innerHTML = state.contacts.map((c) => `<option value="${esc(c.id)}">${esc(c.alias)} · ${esc(REL_LABEL[c.relationship_type] || c.relationship_type)}</option>`).join("");
  if (!state.contacts.length) { $("#meta").textContent = "상대가 없습니다. 관리 화면에서 상대와 대화를 먼저 만드세요."; return; }
  await selectContact(state.contacts.find((c) => c.id === recall()) || state.contacts[0]);
  await loadUsage();
}

$("#login").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const r = await api("POST", "/v1/auth/login", { username: $("#login-user").value.trim(), password: $("#login-pass").value });
    state.csrf = r.csrf_token;
    $("#login-pass").value = "";
    await boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});

async function loadUsage() {
  try {
    const u = await api("GET", "/v1/usage");
    $("#usage").textContent = `오늘 ${u.rewrites} / ${u.rewrite_limit}회`;
  } catch { /* 표시용 */ }
}

async function selectContact(c) {
  state.contact = c;
  state.requestSeq++; // 이전 상대에게 보낸 요청의 응답은 버린다
  remember(c.id);
  $("#contact").value = c.id;
  for (const el of document.querySelectorAll(".who")) el.textContent = c.alias;
  const draft = Object.entries(DEMO_DRAFTS).find(([name]) => c.alias.includes(name))?.[1] || "";
  $("#m-input").value = draft; $("#p-input").value = draft;
  setStage("idle");
  await loadChat(c);
}

// 과거 대화의 마지막 몇 줄을 채팅 배경으로 보여 준다 (실제 메신저를 읽는 것이 아니다).
async function loadChat(c) {
  let msgs = [];
  try {
    const convs = await api("GET", `/v1/conversations?contact_id=${encodeURIComponent(c.id)}`);
    if (convs.length) msgs = (await api("GET", `/v1/conversations/${convs[0].id}/messages`)).slice(-4);
  } catch { /* 배경이 비어도 시연은 된다 */ }
  if (state.contact !== c) return; // 그 사이 상대를 바꿨다
  const html = msgs.map((m) => `<div class="bub ${m.speaker_role === "me" ? "me" : ""}">${esc(m.text)}</div>`).join("");
  $("#m-chat").innerHTML = html; $("#p-chat").innerHTML = html;
}

// ---------- 패널 ----------
function inputEl() { return state.mode === "mobile" ? $("#m-input") : $("#p-input"); }
function panelEl() { return state.mode === "mobile" ? $("#m-panel") : $("#p-pop"); }

function setStage(stage, patch = {}) {
  Object.assign(state, { stage, editing: false }, patch);
  render();
}

// 결과 문장에서 보존 항목이 그대로 나온 구간을 표시한다. 겹치는 구간은 먼저 잡힌 것만 남긴다.
function markPreserved(text, items) {
  const ranges = [];
  for (const it of [...items].sort((a, b) => b.text.length - a.text.length)) {
    if (!it.text) continue;
    const at = text.indexOf(it.text);
    if (at < 0) continue;
    if (ranges.some((r) => at < r.end && at + it.text.length > r.start)) continue;
    ranges.push({ start: at, end: at + it.text.length });
  }
  ranges.sort((a, b) => a.start - b.start);
  let out = ""; let pos = 0;
  for (const r of ranges) { out += `${esc(text.slice(pos, r.start))}<mark>${esc(text.slice(r.start, r.end))}</mark>`; pos = r.end; }
  return out + esc(text.slice(pos));
}

function panelHTML() {
  const pc = state.mode === "pc";
  const name = esc(state.contact?.alias || "");
  const head = `<div class="tool between"><button type="button" class="chip on" data-act="pick">${name}에게 맞게 ▾</button><button type="button" class="chip" data-act="close" aria-label="닫기">✕</button></div>`;
  if (state.stage === "pick") {
    const chips = state.contacts.map((c) => `<button type="button" class="chip ${c.id === state.contact.id ? "on" : ""}" data-contact="${esc(c.id)}">${esc(c.alias)} · ${esc(REL_LABEL[c.relationship_type] || c.relationship_type)}</button>`).join("");
    return `<div class="tool between"><b>누구에게 보내나요?</b><button type="button" class="chip" data-act="close" aria-label="닫기">✕</button></div><div class="tool">${chips}</div><p class="small">키보드는 대화 상대를 알 수 없어 직접 고릅니다. 마지막에 고른 상대가 기본값이 됩니다.</p>`;
  }
  if (state.stage === "loading") {
    return `${head}<div class="skel" role="status"><b>${name}에게 맞게 바꾸는 중…</b><i></i><i></i><i></i></div><p class="small">초안은 입력창에 그대로 있습니다.</p>`;
  }
  if (state.stage === "error") {
    return `${head}<div class="notice"><span class="tag warn">만들지 못했어요</span><p>${esc(state.error)}</p><p>초안은 입력창에 그대로 있습니다.</p></div><div class="btns" style="margin-top:8px"><button type="button" class="btn" data-act="close">닫기</button><button type="button" class="btn pri" data-act="run">다시 시도</button></div>`;
  }
  const r = state.rec.result;
  if (state.stage === "fallback") {
    const why = r.validation.violations.map((v) => `<li>${esc(v.detail)}</li>`).join("") || "<li>모델이 추천 문장을 만들지 못했습니다.</li>";
    return `${head}<div class="notice"><span class="tag warn">초안을 그대로 뒀어요</span><p>사실을 지키지 못한 문장이라 추천하지 않았습니다.</p><ul>${why}</ul></div><div class="btns" style="margin-top:8px"><button type="button" class="btn" data-act="close">닫기</button><button type="button" class="btn pri" data-act="run">다시 만들기</button></div>`;
  }
  const out = state.editing
    ? `<textarea class="out" id="edit-text" aria-label="추천 문장 고치기">${esc(r.result_text)}</textarea>`
    : `<div class="out" id="out-text">${markPreserved(r.result_text, r.preserved_items)}</div>`;
  // 규칙 추출은 '이번 주 금요일'과 '금요일'을 따로 뽑는다. 더 긴 항목에 들어 있는 것은 표시에서 뺀다.
  const texts = [...new Set(r.preserved_items.map((p) => p.text))];
  const shown = texts.filter((t) => !texts.some((o) => o !== t && o.includes(t))).map((text) => ({ text }));
  const facts = shown.map((p) => `<span class="tag ${r.result_text.includes(p.text) ? "ok" : ""}">${r.result_text.includes(p.text) ? "✓ " : ""}${esc(p.text)}</span>`).join("");
  const warns = r.warnings.length ? `<div class="warnbox"><b>확인해 주세요</b><ul>${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
  const key = (k) => (pc ? ` <kbd>${k}</kbd>` : "");
  return `${head}${out}
    ${facts ? `<div class="facts" aria-label="지킨 항목">${facts}</div>` : ""}${warns}
    <div class="tool"><button type="button" class="chip" data-act="run">다시 만들기${key("R")}</button><button type="button" class="chip" data-act="edit">${state.editing ? "고치기 끝" : "직접 고치기"}</button></div>
    <div class="btns"><button type="button" class="btn" data-act="close">초안 그대로${key("Esc")}</button><button type="button" class="btn pri" data-act="insert">입력창에 넣기${key("Enter")}</button></div>
    <p class="small">넣기만 합니다. 전송은 직접 누릅니다.</p>`;
}

function render() {
  const open = state.stage !== "idle";
  $("#m-idle").classList.toggle("hidden", state.mode === "mobile" && open);
  $("#m-panel").classList.toggle("hidden", !(state.mode === "mobile" && open));
  $("#p-pop").classList.toggle("hidden", !(state.mode === "pc" && open));
  if (!open) return;
  panelEl().innerHTML = panelHTML();
  if (state.editing) $("#edit-text")?.focus();
  else if (state.mode === "pc") {
    // 주 행동에 포커스를 둔다: 넣기 → 다시 → 닫기 순 (querySelector의 쉼표 선택자는 문서 순서라 따로 찾는다)
    const p = panelEl();
    (p.querySelector(".btn[data-act=insert]") || p.querySelector(".btn[data-act=run]") || p.querySelector(".btn[data-act=close]"))?.focus();
  }
}

function describe(rec) {
  const r = rec.result;
  const tokens = r.token_usage ? ` · 입력 ${r.token_usage.input_tokens ?? "?"} / 출력 ${r.token_usage.output_tokens ?? "?"} 토큰` : "";
  return `조건 ${rec.condition || "?"} · ${(r.latency_ms / 1000).toFixed(1)}초 · ${r.attempts}회 시도 · 참고한 과거 표현 ${r.evidence_ids.length}개${tokens}`;
}

async function run() {
  const draft = inputEl().value.trim();
  if (!state.contact) return;
  if (!draft) { setStage("error", { error: "입력창에 초안을 먼저 써 주세요." }); return; }
  const seq = ++state.requestSeq; // 늦게 도착한 이전 응답은 버린다
  $("#meta").textContent = "";
  setStage("loading");
  try {
    const rec = await api("POST", "/v1/rewrites",
      { contact_id: state.contact.id, draft_text: draft, situation: $("#situation").value.trim(), condition: $("#condition").value },
      { "Idempotency-Key": crypto.randomUUID() });
    if (seq !== state.requestSeq) return;
    $("#meta").textContent = describe(rec);
    setStage(rec.result.status === "recommended" ? "result" : "fallback", { rec });
  } catch (err) {
    if (seq !== state.requestSeq) return;
    setStage("error", { error: err.status === 429 ? `오늘 사용 한도에 닿았습니다. (${err.message})` : err.message });
  }
  loadUsage();
}

async function insert() {
  if (state.stage !== "result") return;
  const original = state.rec.result.result_text;
  const text = state.editing ? $("#edit-text").value : original;
  const rec = state.rec;
  inputEl().value = text;
  setStage("idle");
  inputEl().focus();
  try {
    await api("POST", `/v1/rewrites/${rec.id}/feedback`, { accepted: true, edited_text: text !== original ? text : null, reason: null });
  } catch (err) { $("#meta").textContent = `피드백 저장 실패: ${err.message}`; }
}

function close() {
  state.requestSeq++; // 진행 중인 요청의 응답을 무시한다
  setStage("idle");
  inputEl().focus();
}

function onPanelClick(e) {
  const contactId = e.target.closest("[data-contact]")?.dataset.contact;
  if (contactId) { selectContact(state.contacts.find((c) => c.id === contactId)).then(run); return; }
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "close") close();
  else if (act === "run") run();
  else if (act === "insert") insert();
  else if (act === "pick") setStage("pick");
  else if (act === "edit") {
    if (state.editing) state.rec.result.result_text = $("#edit-text").value; // 화면 표시용 사본만 고친다
    state.editing = !state.editing; render();
  }
}
$("#m-panel").addEventListener("click", onPanelClick);
$("#p-pop").addEventListener("click", onPanelClick);

// ---------- 모바일 ----------
$("#m-run").addEventListener("click", run);
$("#m-pick").addEventListener("click", () => setStage("pick"));
$("#m-send").addEventListener("click", () => {
  const text = $("#m-input").value.trim();
  if (!text) return;
  $("#m-chat").insertAdjacentHTML("beforeend", `<div class="bub sent">${esc(text)}</div>`);
  $("#m-input").value = "";
});
$(".keys").innerHTML = "<i></i>".repeat(30);

// ---------- PC ----------
$("#p-run").addEventListener("click", run);
function isHotkey(e) {
  if (e.code !== "Space") return false;
  return (e.ctrlKey && e.altKey) || (e.metaKey && e.shiftKey);
}
document.addEventListener("keydown", (e) => {
  if (state.mode === "pc" && isHotkey(e)) { e.preventDefault(); run(); return; }
  if (state.stage === "idle") return;
  if (e.key === "Escape") { e.preventDefault(); close(); return; }
  if (state.mode !== "pc" || state.stage !== "result" || state.editing || e.isComposing) return;
  if (e.target.closest?.("input, textarea, select")) return; // 입력 중인 글자는 가로채지 않는다
  if (e.target.closest?.("button") && e.key === "Enter" && e.target.dataset.act !== "insert") return; // 다른 버튼의 Enter는 그 버튼 몫
  if (e.key === "Enter") { e.preventDefault(); insert(); }
  else if (e.key.toLowerCase() === "r" && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); run(); }
});

// ---------- 공통 설정 ----------
$("#contact").addEventListener("change", (e) => selectContact(state.contacts.find((c) => c.id === e.target.value)));
for (const tab of document.querySelectorAll(".tabs button")) {
  tab.addEventListener("click", () => {
    state.requestSeq++;
    state.mode = tab.dataset.mode;
    for (const t of document.querySelectorAll(".tabs button")) t.setAttribute("aria-selected", String(t === tab));
    $("#stage-mobile").classList.toggle("hidden", state.mode !== "mobile");
    $("#stage-pc").classList.toggle("hidden", state.mode !== "pc");
    setStage("idle");
  });
}

boot();
