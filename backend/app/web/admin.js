// 운영 대시보드 (와이어프레임 A1). 같은 출처의 세션 쿠키로 읽기만 한다.
const $ = (selector) => document.querySelector(selector);

const REASONS = {
  preserve_missing: "날짜·수량·약속 등 지켜야 할 내용 누락",
  fact_added: "초안에 없던 내용 추가",
  negation_changed: "가능/불가 뒤바뀜",
  forbidden_phrase: "금지 표현 사용",
  evidence_unknown: "근거 표시 오류",
  empty: "빈 응답",
  refusal: "모델 거절",
  unknown: "사유 기록 없음 (모델 장애·시간 초과 등)",
};

const number = (value) => new Intl.NumberFormat("ko-KR").format(value);
const percent = (rate) => (rate === null ? "–" : `${(rate * 100).toFixed(1)}%`);
const seconds = (ms) => (ms === null ? "–" : `${(ms / 1000).toFixed(1)}초`);

function notice(text, link) {
  const box = $("#notice");
  box.replaceChildren(document.createTextNode(text));
  if (link) {
    const a = document.createElement("a");
    a.href = link.href;
    a.textContent = link.text;
    box.append(" ", a);
  }
  box.hidden = false;
  $("#board").hidden = true;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function render(data) {
  $("#t-requests").textContent = number(data.requests);
  $("#t-users").textContent = `활성 사용자 ${number(data.active_users)}명`;
  $("#t-success").textContent = percent(data.success_rate);
  $("#t-kept").textContent = `원문 유지 ${number(data.kept)}건`;
  $("#t-p95").textContent = seconds(data.latency_p95_ms);
  $("#t-p50").textContent = `중앙값 ${seconds(data.latency_p50_ms)}`;
  $("#t-accept").textContent = percent(data.accept_rate);
  $("#t-feedback").textContent = `응답 ${number(data.feedback_total)}건`;
  $("#t-tokens").textContent = number(data.input_tokens + data.output_tokens);
  $("#t-token-split").textContent = `입력 ${number(data.input_tokens)} · 출력 ${number(data.output_tokens)}`;

  const reasons = $("#reasons");
  reasons.replaceChildren();
  const most = Math.max(1, ...data.fallback_reasons.map((r) => r.count));
  for (const reason of data.fallback_reasons) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = REASONS[reason.check] ?? reason.check;
    const count = document.createElement("span");
    count.className = "n";
    count.textContent = `${number(reason.count)}건`;
    const track = document.createElement("span");
    track.className = "track";
    const fill = document.createElement("span");
    fill.className = "fill";
    fill.style.width = `${(reason.count / most) * 100}%`;
    track.append(fill);
    li.append(name, count, track);
    reasons.append(li);
  }
  $("#reasons-empty").hidden = data.fallback_reasons.length > 0;

  const conditions = $("#conditions");
  conditions.replaceChildren();
  for (const row of data.by_condition) {
    const tr = document.createElement("tr");
    tr.append(cell(row.condition), cell(number(row.requests), "num"), cell(number(row.kept), "num"));
    conditions.append(tr);
  }

  const pending = $("#s-pending");
  pending.textContent = data.graph_enabled ? `${number(data.graph_pending_messages)}건` : "그래프 사용 안 함";
  pending.className = data.graph_enabled && data.graph_pending_messages > 0 ? "attention" : "";
  const failed = $("#s-failed");
  failed.textContent = `${number(data.graph_failed_jobs)}건`;
  failed.className = data.graph_failed_jobs > 0 ? "broken" : "";
  $("#s-limit").textContent = `${number(data.users_at_limit_today)}명 (하루 ${number(data.daily_rewrite_limit)}회)`;

  const rejections = $("#rejections");
  rejections.replaceChildren();
  for (const row of data.recent_rejections) {
    const tr = document.createElement("tr");
    const at = new Date(row.at).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
    tr.append(cell(row.user), cell(row.condition), cell(row.has_reason ? "있음" : "없음"), cell(at));
    rejections.append(tr);
  }
  $("#rejections-table").hidden = data.recent_rejections.length === 0;
  $("#rejections-empty").hidden = data.recent_rejections.length > 0;

  const stamp = new Date(data.generated_at).toLocaleString("ko-KR");
  $("#stamp").textContent = `${stamp} 기준 · 최근 ${data.days}일` + (data.truncated ? " · 건수가 많아 최근 2만 건만 집계했습니다" : "");
  $("#notice").hidden = true;
  $("#board").hidden = false;
}

async function loadHealth() {
  const target = $("#s-health");
  try {
    const response = await fetch("/healthz?deep=true");
    const body = await response.json();
    const word = (state) => (state === "ok" ? "정상" : state === "skipped" ? "사용 안 함" : "오류");
    const checks = Object.entries(body.checks ?? {}).map(([name, state]) => `${name} ${word(state)}`);
    target.textContent = checks.length ? checks.join(" · ") : body.status;
    target.className = response.ok ? "ok" : "broken";
  } catch {
    target.textContent = "확인 실패";
    target.className = "broken";
  }
}

async function load(days) {
  for (const button of document.querySelectorAll(".range button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.days) === days));
  }
  let response;
  try {
    response = await fetch(`/v1/admin/overview?days=${days}`, { credentials: "same-origin" });
  } catch {
    notice("서버에 연결하지 못했습니다. 잠시 뒤 다시 열어 주세요.");
    return;
  }
  if (response.status === 401) return notice("로그인이 필요합니다.", { href: "/", text: "로그인하러 가기" });
  if (response.status === 403) return notice("이 계정에는 운영 권한이 없습니다. 서버의 ADMIN_USERNAMES 설정에 아이디를 넣어야 합니다.");
  if (!response.ok) return notice("집계를 불러오지 못했습니다. 서버 로그를 확인해 주세요.");
  render(await response.json());
  loadHealth();
}

for (const button of document.querySelectorAll(".range button")) {
  button.addEventListener("click", () => load(Number(button.dataset.days)));
}
load(7);
