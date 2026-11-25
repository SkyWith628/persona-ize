// 운영 대시보드(/admin)의 시작점. 빌드 없는 ES 모듈이고, 관리자 API(/v1/admin/*)를 읽는다.
// 쓰기는 두 가지다: "다시 시도"(보류된 그래프 메시지 — 수집 표는 전체, 보류 표는 대화방 하나)와 회원 관리(승인·정지·권한·세션 끊기·삭제)
// — 모두 확인을 받은 뒤에만 보내고, CSRF 토큰은 누를 때마다 `/v1/auth/me`에서 새로 읽는다.
// 화면을 열거나 새로 고칠 때마다 서버의 관리자 접근 기록에 남는다 — 그래서 자동 새로 고침은 하지 않는다.

import { getJson, sendJson } from "./admin/api.js";
import { confirmDialog } from "./admin/dialog.js";
import {
  clock,
  gateFor,
  lastPageOffset,
  memberActionDoneText,
  memberActionErrorText,
  memberActionPlan,
  memberFilterText,
  membersJumpText,
  membersPageText,
  membersQuery,
  num,
  rangeText,
  retryBody,
  retryConfirmText,
  retryErrorText,
  retryResultText,
} from "./admin/format.js";
import {
  renderAccess,
  renderFailure,
  renderFeedback,
  renderHeld,
  renderJobs,
  renderMembers,
  renderOverview,
  renderUsers,
} from "./admin/view.js";

const LIST_LIMIT = 50; // 거절 피드백·가져오기 작업
const USERS_PAGE = 50;
const MEMBERS_PAGE = 50;
const ACCESS_LIMIT = 200; // 묶어서 보여 주므로 넉넉히 읽는다

const $ = (id) => document.getElementById(id);

const roots = {
  kpis: $("kpis"),
  daily: $("daily"),
  reasons: $("reasons"),
  mix: $("mix"),
  cost: $("cost"),
  ingest: $("ingest"),
};

const state = {
  days: 7,
  usersOffset: 0,
  overviewSeq: 0, // 기간을 빠르게 바꿔도 마지막 요청의 응답만 그린다
  usersSeq: 0,
  heldSeq: 0,
  graphEnabled: false, // 마지막 요약 기준 — 꺼진 서버에서는 보류 표의 다시 시도 버튼을 내지 않는다
  // 회원 관리: 검색어·필터·쪽. 검색어는 화면 상태로만 둔다(주소·브라우저 저장소에 넣지 않는다 — 이메일일 수 있다)
  members: { query: "", status: "", role: "", offset: 0 },
  membersSeq: 0,
  me: null, // 지금 로그인한 관리자({user_id, username}) — 자기 줄의 버튼을 막는 데 쓴다
  who: Promise.resolve(), // 진행 중인 showWho (회원 표는 이것이 끝난 뒤에 그린다)
  loadedOnce: false,
  pending: 0, // 진행 중인 불러오기 수 (0이 되면 '바쁨' 표시를 뗀다)
};

/** 진행·오류 안내. quiet이면 화면에는 보이지 않고 화면 낭독기에만 알린다(끝났다는 알림처럼). */
function setStatus(text, { error = false, quiet = false } = {}) {
  const node = $("status");
  node.textContent = text;
  node.classList.toggle("error", error);
  node.classList.toggle("sr-only", quiet);
}

/** 접근이 막혔을 때: 대시보드를 치우고 이유와 갈 곳을 보여 준다. */
function showGate(gate) {
  $("dash").hidden = true;
  $("gate-title").textContent = gate.title;
  $("gate-text").textContent = gate.text;
  $("gate-link").textContent = gate.link;
  $("gate-link").setAttribute("href", gate.href ?? "/");
  $("gate").hidden = false;
  // 보이는 안내는 gate가 맡고, status는 화면 낭독기에만 같은 내용을 알린다.
  setStatus(gate.title, { quiet: true });
}

/** 오류 처리. 접근 문제면 안내 화면으로 바꾸고 true를 돌려준다. */
function handleError(error) {
  const gate = gateFor(error);
  if (gate) {
    showGate(gate);
    // 로그인은 돼 있는데 막힌 경우(403·503)에는 어느 계정인지 보여 준다
    if (error.status !== 401) showWho();
    else $("who").textContent = "";
    return true;
  }
  if (!state.loadedOnce) {
    // 한 번도 그리지 못했으면 빈 화면 대신 무엇이 문제인지와 다시 시도할 길을 준다
    showGate({ title: "불러오지 못했습니다", text: error.message, link: "다시 시도", href: "/admin" });
    return false;
  }
  setStatus(`불러오지 못했습니다. ${error.message}`, { error: true });
  return false;
}

/** 불러오는 동안 대시보드에 aria-busy를 건다. 화면에서는 살짝 흐려진다(자리 이동 없음). */
function busy(delta) {
  state.pending = Math.max(0, state.pending + delta);
  if (state.pending > 0) $("dash").setAttribute("aria-busy", "true");
  else $("dash").removeAttribute("aria-busy");
}

function markUpdated() {
  $("updated").textContent = `· 마지막 갱신 ${clock(new Date())}`;
}

/** 요약을 읽어 그린다. 그 사이 더 새로운 요청이 나갔으면 그리지 않고 false를 돌려준다. */
async function loadOverview() {
  const seq = ++state.overviewSeq;
  const overview = await getJson(`/v1/admin/overview?days=${state.days}`);
  if (seq !== state.overviewSeq) return false;
  state.graphEnabled = Boolean(overview.graph?.enabled);
  renderOverview(roots, overview, { onRetryHeld: retryHeld });
  $("range-text").textContent = rangeText(overview);
  return true;
}

/** 보류 표(대화방별)를 읽어 그린다. 그 사이 더 새로운 요청이 나갔으면 그리지 않는다. */
async function loadHeld() {
  const seq = ++state.heldSeq;
  const items = await getJson(`/v1/admin/graph/held?limit=${LIST_LIMIT}`);
  if (seq !== state.heldSeq) return;
  renderHeld($("t-held"), items, {
    canRetry: state.graphEnabled,
    onRetry: (group, button) => retryHeld(group.messages, button, group),
  });
}

/** 보류 표 구획의 안내(줄의 다시 시도 결과·오류). 빈 글자면 자리를 차지하지 않는다. */
function setHeldNote(text, { error = false } = {}) {
  const node = $("held-note");
  node.textContent = text;
  node.classList.toggle("error", error);
}

/**
 * "다시 시도": 확인 → 보류 풀기(POST) → 결과 안내 → 요약과 보류 표를 새로 읽는다.
 * `group`(보류 표의 한 줄)을 주면 그 대화방만, 없으면(수집 표의 버튼) 전체를 푼다.
 * 수집은 서버가 뒤에서 하므로 바로 끝난다. 풀린 메시지는 요약의 "기다리는 메시지"로 옮겨 보인다.
 */
async function retryHeld(count, button, group = null) {
  if (!window.confirm(retryConfirmText(count, group))) return;
  button.disabled = true; // 두 번 눌러 두 번 보내지 않게. 성공하면 요약을 다시 그리며 버튼이 사라진다
  busy(+1);
  setStatus("보류된 메시지를 다시 시도하는 중입니다…", { quiet: true });
  let text;
  try {
    // 토큰은 누를 때마다 새로 읽는다 (다른 탭에서 다시 로그인했을 수 있다)
    const me = await getJson("/v1/auth/me");
    text = retryResultText(
      await sendJson("POST", "/v1/admin/graph/retry-held", me.csrf_token, retryBody(group)),
    );
  } catch (error) {
    busy(-1);
    button.disabled = false;
    if (error.status === 401) handleError(error);
    else if (group) setHeldNote(retryErrorText(error), { error: true });
    else setStatus(retryErrorText(error), { error: true });
    return;
  }
  try {
    if (await loadOverview()) markUpdated();
    // 풀린 줄이 표에서 빠진다 (누른 버튼도 함께 사라진다)
    if (await loadInto($("t-held"), loadHeld)) return;
  } catch (error) {
    if (handleError(error)) return; // 접근이 막혔으면 안내 화면이 대신한다
  } finally {
    busy(-1);
  }
  setStatus(text, { quiet: Boolean(group) });
  // 버튼이 있던 자리에도 결과를 남긴다 (위쪽 안내는 화면 밖일 수 있다)
  if (group) {
    setHeldNote(text);
    $("h-held").focus(); // 누른 버튼은 줄과 함께 사라졌다
    return;
  }
  setHeldNote("");
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = text;
  roots.ingest.append(note);
}

function renderUsersPager(page) {
  const to = state.usersOffset + page.items.length;
  $("users-page").textContent = page.items.length
    ? `${num(state.usersOffset + 1)}–${num(to)} / 전체 ${num(page.total)}명`
    : `전체 ${num(page.total)}명`;
  $("users-prev").disabled = state.usersOffset <= 0;
  $("users-next").disabled = to >= page.total;
}

async function loadUsers() {
  const seq = ++state.usersSeq;
  // 응답이 오기 전에 또 눌러 없는 쪽으로 넘어가지 않게 잠근다 (응답이 오면 다시 정한다)
  $("users-prev").disabled = true;
  $("users-next").disabled = true;
  const page = await getJson(`/v1/admin/users?limit=${USERS_PAGE}&offset=${state.usersOffset}`);
  if (seq !== state.usersSeq) return;
  renderUsers($("t-users"), page);
  renderUsersPager(page);
}

// ---------- 회원 관리 ----------

const memberChips = () => document.querySelectorAll(".chip[data-status]");

/** 회원 관리 구획의 안내(결과·오류). 빈 글자면 자리를 차지하지 않는다. */
function setMembersNote(text, { error = false } = {}) {
  const node = $("members-note");
  node.textContent = text;
  node.classList.toggle("error", error);
}

function membersFiltered() {
  const { query, status, role } = state.members;
  return Boolean(query.trim() || status || role);
}

/** 상태별 수(필터와 무관한 전체 수)를 필터 버튼과 위쪽 바로 가기에 적는다. */
function renderMemberCounts(counts) {
  for (const chip of memberChips()) {
    chip.textContent = memberFilterText(chip.dataset.status, counts);
    chip.setAttribute("aria-pressed", String(chip.dataset.status === state.members.status));
  }
  $("members-jump").textContent = membersJumpText(counts);
}

function renderMembersPager(page) {
  const shown = page.items.length;
  const to = state.members.offset + shown;
  $("members-page").textContent = membersPageText(state.members.offset, shown, page.total, membersFiltered());
  $("members-prev").disabled = state.members.offset <= 0;
  $("members-next").disabled = to >= page.total;
}

/** 회원 목록을 읽어 그린다. 그 사이 더 새로운 요청이 나갔으면 그리지 않는다. */
async function loadMembers() {
  const seq = ++state.membersSeq;
  $("members-prev").disabled = true;
  $("members-next").disabled = true;
  await state.who; // 내가 누구인지 안 뒤에 그린다 (자기 줄의 정지·삭제 버튼을 막아야 한다)
  const query = membersQuery({ ...state.members, limit: MEMBERS_PAGE });
  const page = await getJson(`/v1/admin/members?${query}`);
  if (seq !== state.membersSeq) return;
  // 삭제·필터 변화로 지금 쪽이 비었으면 마지막 쪽으로 물러난다
  if (!page.items.length && page.total > 0 && state.members.offset > 0) {
    state.members.offset = lastPageOffset(page.total, MEMBERS_PAGE);
    await loadMembers();
    return;
  }
  renderMembers($("t-members"), page, {
    selfId: state.me?.user_id ?? null,
    onAction: memberAction,
    filtered: membersFiltered(),
  });
  renderMemberCounts(page.counts);
  renderMembersPager(page);
}

/** 회원 목록만 다시 읽는다(검색·필터·쪽 이동 뒤). 접근이 막혔으면 true. */
async function reloadMembers() {
  busy(+1);
  try {
    return await loadInto($("t-members"), loadMembers);
  } finally {
    busy(-1);
  }
}

// 누른 버튼이 바뀌어 나타나는 짝 (정지 ↔ 정지 해제, 관리자 지정 ↔ 해제) — 작업 뒤 포커스를 그 자리에 둔다
const ACTION_PAIR = { suspend: "activate", activate: "suspend", promote: "demote", demote: "promote" };

/**
 * 작업 뒤 포커스: 같은 회원의 같은 버튼 → 그 자리에 바뀌어 나타난 짝 → 그 회원의 첫 버튼 → 구획 제목
 * (목록을 다시 그려 누른 버튼은 사라졌다. 삭제했으면 그 줄이 없으므로 제목으로 간다).
 */
function focusMember(userId, key) {
  const buttons = [...$("t-members").querySelectorAll("button[data-user]")].filter(
    (b) => b.dataset.user === userId && !b.disabled,
  );
  const by = (action) => buttons.find((b) => b.dataset.action === action);
  const target = by(key) ?? by(ACTION_PAIR[key]) ?? buttons[0] ?? $("h-members");
  target.focus();
}

/**
 * 회원 줄의 버튼: 확인 → 요청(CSRF 토큰과 함께) → 결과 안내 → 목록과 상태별 수를 새로 읽는다.
 * 서버 규칙에 걸리면(409 cannot_modify_self·protected_admin·last_admin) 그 까닭을 구획 안내에 적는다.
 * 그 사이 다른 관리자가 상태를 바꿨으면(409 member_changed) 새로 읽으라는 안내를 적는다 — 어느 쪽이든
 * 아래에서 목록을 다시 읽어 지금 상태를 보여 준다.
 */
async function memberAction(key, member, button) {
  const plan = memberActionPlan(key, member, state.me?.user_id ?? null);
  if (!(await confirmDialog(plan.confirm))) return;
  button.disabled = true; // 두 번 눌러 두 번 보내지 않게. 끝나면 목록을 다시 그린다
  busy(+1);
  setMembersNote("처리하는 중입니다…");
  let text;
  let failed = false;
  try {
    // 토큰은 누를 때마다 새로 읽는다 (다른 탭에서 다시 로그인했을 수 있다)
    const me = await getJson("/v1/auth/me");
    text = memberActionDoneText(key, member, await sendJson(plan.method, plan.path, me.csrf_token, plan.body));
  } catch (error) {
    // 로그인이 끊겼거나(401) 저장소가 없으면(503 admin_unavailable) 안내 화면이 대신한다
    if (error.status === 401 || (error.status === 503 && gateFor(error))) {
      busy(-1);
      handleError(error);
      return;
    }
    text = memberActionErrorText(key, error);
    failed = true;
  }
  // 성공했든 실패했든 목록을 새로 읽는다 — 실패는 화면이 낡아서일 수 있다(이미 삭제·이미 바뀜)
  let blocked;
  try {
    blocked = await loadInto($("t-members"), loadMembers);
  } finally {
    busy(-1);
  }
  if (blocked) return; // 접근이 막혔으면 안내 화면이 대신한다 (내 세션을 끊은 경우 등)
  setMembersNote(text, { error: failed });
  focusMember(member.user_id, key);
}

async function changeMembersPage(step) {
  state.members.offset = Math.max(0, state.members.offset + step * MEMBERS_PAGE);
  setMembersNote("");
  if (await reloadMembers()) return;
  $("h-members").focus(); // 쪽이 바뀌면 구획 맨 위에서 다시 읽게 한다 (누른 버튼이 비활성이 될 수 있다)
}

/** 검색·필터를 바꾸면 첫 쪽부터 다시 읽는다. 결과 안내는 지운다(이전 작업의 것이다). */
async function changeMembersFilter(patch) {
  Object.assign(state.members, patch, { offset: 0 });
  for (const chip of memberChips()) {
    chip.setAttribute("aria-pressed", String(chip.dataset.status === state.members.status));
  }
  setMembersNote("");
  await reloadMembers();
}

/** 표 하나를 불러와 그린다. 실패해도 다른 표는 그대로 두고, 접근 문제면 true를 돌려준다. */
async function loadInto(root, task) {
  try {
    await task();
    return false;
  } catch (error) {
    if (gateFor(error)) return handleError(error);
    renderFailure(root, error.message);
    return false;
  }
}

async function loadAll() {
  const refresh = $("refresh");
  refresh.disabled = true;
  busy(+1);
  // 처음에는 화면에 보이게, 그 뒤로는 낭독기에만 알린다 (대시보드가 흐려지는 것으로 충분하다)
  setStatus(state.loadedOnce ? "새로 고치는 중입니다…" : "불러오는 중입니다…", { quiet: state.loadedOnce });
  try {
    // 요약을 먼저 읽는다. 로그인·권한·저장소 문제는 여기서 걸린다.
    try {
      await loadOverview();
    } catch (error) {
      handleError(error);
      return;
    }
    $("gate").hidden = true;
    $("dash").hidden = false;
    state.who = showWho(); // 로그인이 확인된 뒤에만 묻는다 (로그인 전에는 실패하는 요청을 하나라도 덜 보낸다)

    const blocked = await Promise.all([
      loadInto($("t-feedback"), async () =>
        renderFeedback($("t-feedback"), await getJson(`/v1/admin/feedback?limit=${LIST_LIMIT}`)),
      ),
      loadInto($("t-jobs"), async () =>
        renderJobs($("t-jobs"), await getJson(`/v1/admin/jobs?limit=${LIST_LIMIT}`)),
      ),
      loadInto($("t-held"), loadHeld),
      loadInto($("t-users"), loadUsers),
      loadInto($("t-members"), loadMembers),
    ]);
    if (blocked.some(Boolean)) return;
    // 접근 기록은 마지막에 읽는다 — 방금 한 호출들이 목록에 보이도록.
    const accessBlocked = await loadInto($("t-access"), async () =>
      renderAccess($("t-access"), await getJson(`/v1/admin/access-log?limit=${ACCESS_LIMIT}`)),
    );
    if (accessBlocked) return;

    markUpdated();
    setStatus(state.loadedOnce ? "새로 고쳤습니다." : "", { quiet: true });
    state.loadedOnce = true;
  } finally {
    refresh.disabled = false;
    busy(-1);
  }
}

async function changeDays(days) {
  state.days = days;
  for (const chip of document.querySelectorAll(".chip[data-days]")) {
    chip.setAttribute("aria-pressed", String(Number(chip.dataset.days) === days));
  }
  setStatus("기간을 바꾸는 중입니다…", { quiet: true });
  busy(+1);
  try {
    if (await loadOverview()) {
      markUpdated();
      setStatus("기간을 바꿨습니다.", { quiet: true });
    }
  } catch (error) {
    handleError(error);
  } finally {
    busy(-1);
  }
}

async function changeUsersPage(step) {
  state.usersOffset = Math.max(0, state.usersOffset + step * USERS_PAGE);
  await loadInto($("t-users"), loadUsers);
}

/** 지금 로그인한 계정 이름. 새로 고칠 때마다 다시 묻는다(다른 탭에서 계정을 바꿨을 수 있다). */
async function showWho() {
  try {
    const me = await getJson("/v1/auth/me");
    state.me = { user_id: me.user_id, username: me.username };
    $("who").textContent = `${me.username} 님`;
  } catch {
    state.me = null; // 내 줄을 알아보지 못해도 서버가 자기 자신 정지·삭제를 막는다
    $("who").textContent = ""; // 이름은 없어도 화면은 쓸 수 있다
  }
}

for (const chip of document.querySelectorAll(".chip[data-days]")) {
  chip.addEventListener("click", () => changeDays(Number(chip.dataset.days)));
}
for (const chip of memberChips()) {
  chip.addEventListener("click", () => changeMembersFilter({ status: chip.dataset.status }));
}
// 검색은 '검색'을 누르거나 Enter를 칠 때만 한다 — 글자마다 찾으면 호출마다 접근 기록이 남는다.
$("members-search").addEventListener("submit", (event) => {
  event.preventDefault();
  changeMembersFilter({ query: $("members-query").value, role: $("members-role").value });
});
$("members-role").addEventListener("change", () =>
  changeMembersFilter({ query: $("members-query").value, role: $("members-role").value }),
);
$("members-prev").addEventListener("click", () => changeMembersPage(-1));
$("members-next").addEventListener("click", () => changeMembersPage(1));
$("refresh").addEventListener("click", () => loadAll());
$("users-prev").addEventListener("click", () => changeUsersPage(-1));
$("users-next").addEventListener("click", () => changeUsersPage(1));

loadAll();
