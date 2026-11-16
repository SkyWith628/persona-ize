// 운영 대시보드(/admin)의 시작점. 빌드 없는 ES 모듈이고, 관리자 API(/v1/admin/*)를 읽는다.
// 쓰기는 하나뿐이다: 수집 표의 "다시 시도"(보류된 그래프 메시지) — 확인을 받은 뒤에만 보낸다.
// 화면을 열거나 새로 고칠 때마다 서버의 관리자 접근 기록에 남는다 — 그래서 자동 새로 고침은 하지 않는다.

import { getJson, postJson } from "./admin/api.js";
import {
  clock,
  gateFor,
  num,
  rangeText,
  retryConfirmText,
  retryErrorText,
  retryResultText,
} from "./admin/format.js";
import {
  renderAccess,
  renderFailure,
  renderFeedback,
  renderJobs,
  renderOverview,
  renderUsers,
} from "./admin/view.js";

const LIST_LIMIT = 50; // 거절 피드백·가져오기 작업
const USERS_PAGE = 50;
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
  renderOverview(roots, overview, { onRetryHeld: retryHeld });
  $("range-text").textContent = rangeText(overview);
  return true;
}

/**
 * 수집 표의 "다시 시도": 확인 → 보류 풀기(POST) → 결과 안내 → 요약을 새로 읽는다.
 * 수집은 서버가 뒤에서 하므로 바로 끝난다. 풀린 메시지는 요약의 "기다리는 메시지"로 옮겨 보인다.
 */
async function retryHeld(count, button) {
  if (!window.confirm(retryConfirmText(count))) return;
  button.disabled = true; // 두 번 눌러 두 번 보내지 않게. 성공하면 요약을 다시 그리며 버튼이 사라진다
  busy(+1);
  setStatus("보류된 메시지를 다시 시도하는 중입니다…", { quiet: true });
  let text;
  try {
    // 토큰은 누를 때마다 새로 읽는다 (다른 탭에서 다시 로그인했을 수 있다)
    const me = await getJson("/v1/auth/me");
    text = retryResultText(await postJson("/v1/admin/graph/retry-held", me.csrf_token));
  } catch (error) {
    busy(-1);
    button.disabled = false;
    if (error.status === 401) handleError(error);
    else setStatus(retryErrorText(error), { error: true });
    return;
  }
  try {
    if (await loadOverview()) markUpdated();
  } catch (error) {
    if (handleError(error)) return; // 접근이 막혔으면 안내 화면이 대신한다
  } finally {
    busy(-1);
  }
  setStatus(text);
  // 버튼이 있던 자리에도 결과를 남긴다 (위쪽 안내는 화면 밖일 수 있다)
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
    showWho(); // 로그인이 확인된 뒤에만 묻는다 (로그인 전에는 실패하는 요청을 하나라도 덜 보낸다)

    const blocked = await Promise.all([
      loadInto($("t-feedback"), async () =>
        renderFeedback($("t-feedback"), await getJson(`/v1/admin/feedback?limit=${LIST_LIMIT}`)),
      ),
      loadInto($("t-jobs"), async () =>
        renderJobs($("t-jobs"), await getJson(`/v1/admin/jobs?limit=${LIST_LIMIT}`)),
      ),
      loadInto($("t-users"), loadUsers),
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
  for (const chip of document.querySelectorAll(".chip")) {
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
    $("who").textContent = `${me.username} 님`;
  } catch {
    $("who").textContent = ""; // 이름은 없어도 화면은 쓸 수 있다
  }
}

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => changeDays(Number(chip.dataset.days)));
}
$("refresh").addEventListener("click", () => loadAll());
$("users-prev").addEventListener("click", () => changeUsersPage(-1));
$("users-next").addEventListener("click", () => changeUsersPage(1));

loadAll();
