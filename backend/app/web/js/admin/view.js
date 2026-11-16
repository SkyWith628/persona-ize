// 관리자 화면 그리기. 서버가 준 문자열은 전부 텍스트 노드로 넣는다(innerHTML을 쓰지 않는다).
// 차트는 라이브러리 없이 DOM과 CSS로 그리고, 같은 자료를 표로도 준다.

import {
  ACTION_LABEL,
  CHANNEL_LABEL,
  CHECK_LABEL,
  CONDITION_LABEL,
  GRAPH_STATUS_LABEL,
  JOB_STATUS_LABEL,
  NO_DATA,
  REASON_LABEL,
  RELATION_LABEL,
  axisStep,
  costSummary,
  dateTime,
  dayLong,
  dayShort,
  duration,
  groupAccess,
  healthView,
  heldView,
  label,
  num,
  percent,
  share,
  usd,
} from "./format.js";

/** 요소 하나를 만든다. children의 문자열은 텍스트 노드가 된다. */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

function fill(root, ...nodes) {
  root.replaceChildren(...nodes.filter((n) => n !== null && n !== undefined && n !== false));
}

function empty(text) {
  return el("p", { class: "empty", text });
}

function hint(text) {
  return el("p", { class: "hint muted", text });
}

const TAG_ICON = { ok: "✓", warn: "!", bad: "×", off: "–" };

/** 상태 표시. 색만이 아니라 기호와 문구를 함께 쓴다. */
function tag(kind, text) {
  return el("span", { class: `tag ${kind}` }, [
    el("span", { "aria-hidden": "true", text: TAG_ICON[kind] ?? "" }),
    text,
  ]);
}

function timeCell(iso) {
  return iso ? el("time", { datetime: iso, text: dateTime(iso) }) : "–";
}

function idCell(shortId) {
  return el("code", { text: shortId });
}

// ---------- 핵심 지표 ----------

function kpi(name, value, sub, isNone = false) {
  return el("li", { class: "kpi" }, [
    el("span", { class: "kpi-label", text: name }),
    el("strong", { class: isNone ? "kpi-value none" : "kpi-value", text: value }),
    el("span", { class: "kpi-sub", text: sub }),
  ]);
}

export function renderKpis(root, overview) {
  const { users, rewrites, latency_ms: latency, feedback } = overview;
  const success = percent(rewrites.success_rate);
  const p95 = duration(latency.p95);
  const accepted = percent(feedback.acceptance_rate);
  const cost = costSummary(overview);
  const unregistered =
    users.active_unregistered > 0 ? ` (등록 안 된 id ${num(users.active_unregistered)}개 포함)` : "";
  fill(
    root,
    kpi("첨삭 요청", `${num(rewrites.total)}건`, `활성 사용자 ${num(users.active)}명${unregistered}`),
    kpi(
      "추천 성공률",
      success ?? NO_DATA,
      success
        ? `원문 유지 ${percent(share(rewrites.fallback, rewrites.total))} · ${num(rewrites.fallback)}건`
        : "이 기간에 요청이 없습니다",
      !success,
    ),
    kpi(
      "응답 시간 p95",
      p95 ?? NO_DATA,
      p95 ? `중앙값 ${duration(latency.p50)}` : "이 기간에 요청이 없습니다",
      !p95,
    ),
    kpi(
      "적용 비율 (피드백 기준)",
      accepted ?? NO_DATA,
      accepted
        ? `피드백 ${num(feedback.total)}건 중 ${num(feedback.accepted)}건 적용`
        : "이 기간에 피드백이 없습니다",
      !accepted,
    ),
    kpi("기간 비용", cost.value, cost.sub, !cost.known),
  );
}

// ---------- 일별 막대 ----------

function bar(day, max, showValue) {
  const item = el("li", {
    class: "bar",
    title: `${dayLong(day.day)} · 요청 ${num(day.total)}건 · 원문 유지 ${num(day.fallback)}건`,
  });
  if (day.total === 0) return item;
  // 높이는 가장 많은 날 대비, 빗금 부분은 그날 요청 대비. 아주 작은 값도 보이게 최소 높이를 둔다.
  item.style.setProperty("--h", `${Math.max((day.total / max) * 100, 2)}%`);
  item.style.setProperty("--f", `${(day.fallback / day.total) * 100}%`);
  if (showValue) item.append(el("span", { class: "bar-value", text: num(day.total) }));
  item.append(el("span", { class: "bar-stack" }, day.fallback > 0 ? [el("span", { class: "seg-fallback" })] : []));
  return item;
}

export function renderDaily(root, overview) {
  const days = overview.rewrites.by_day;
  const total = overview.rewrites.total;
  const max = Math.max(0, ...days.map((d) => d.total));
  if (max === 0) {
    fill(root, empty(`${NO_DATA} — 이 기간에 들어온 첨삭 요청이 없습니다.`));
    return;
  }
  const busiest = days.reduce((best, d) => (d.total > best.total ? d : best), days[0]);
  const step = axisStep(days.length);
  const last = days.length - 1;
  const summary =
    days.length === 1
      ? `요청 ${num(total)}건 · 원문 유지 ${num(overview.rewrites.fallback)}건`
      : `합계 ${num(total)}건 · 하루 평균 ${num(Math.round((total / days.length) * 10) / 10)}건 · ` +
        `가장 많은 날 ${dayLong(busiest.day)} ${num(busiest.total)}건`;

  const chart = el("div", { class: days.length > 10 ? "bars dense" : "bars", "aria-hidden": "true" }, [
    el("ol", { class: "bars-plot" }, days.map((d) => bar(d, max, days.length <= 10))),
    el(
      "ol",
      { class: "bars-axis" },
      days.map((d, i) => el("li", { text: (last - i) % step === 0 ? dayShort(d.day) : "" })),
    ),
  ]);
  const legend = el("p", { class: "legend" }, [
    el("span", { class: "legend-item" }, [el("i", { class: "swatch ok", "aria-hidden": "true" }), "추천 성공"]),
    el("span", { class: "legend-item" }, [
      el("i", { class: "swatch fallback", "aria-hidden": "true" }),
      "원문 유지 (빗금)",
    ]),
  ]);
  const rows = days.map((d) =>
    el("tr", {}, [
      el("th", { scope: "row", text: dayLong(d.day) }),
      el("td", { class: "num", text: `${num(d.total)}건` }),
      el("td", { class: "num", text: `${num(d.total - d.fallback)}건` }),
      el("td", { class: "num", text: `${num(d.fallback)}건` }),
      el("td", { class: "num", text: percent(share(d.fallback, d.total)) ?? "–" }),
    ]),
  );
  // 새로 고치거나 기간을 바꿔도 펼쳐 둔 표는 펼친 채로 둔다
  const wasOpen = root.querySelector("details.as-table")?.open === true;
  const asTable = el("details", { class: "as-table", open: wasOpen }, [
    el("summary", { text: "표로 보기" }),
    el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "일별 첨삭 요청 표" }, [
      el("table", {}, [
        el("caption", { class: "sr-only", text: "일별 첨삭 요청" }),
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col", text: "날짜" }),
            el("th", { scope: "col", class: "num", text: "요청" }),
            el("th", { scope: "col", class: "num", text: "추천 성공" }),
            el("th", { scope: "col", class: "num", text: "원문 유지" }),
            el("th", { scope: "col", class: "num", text: "원문 유지 비율" }),
          ]),
        ]),
        el("tbody", {}, rows),
      ]),
    ]),
  ]);
  fill(root, el("p", { class: "chart-summary", text: summary }), chart, legend, asTable);
}

// ---------- 분포 (막대 목록) ----------

/** 이름 · 값 · 전체에 대한 몫을 한 줄과 가는 막대로. part가 null이면 몫을 적지 않는다. */
function meterItem(name, valueText, part) {
  const pct = percent(part);
  const gauge = el("i");
  gauge.style.width = `${Math.min(Math.max((part ?? 0) * 100, 0), 100)}%`;
  return el("li", { class: "meter-item" }, [
    el("div", { class: "meter-head" }, [
      el("span", { text: name }),
      el("span", { class: "meter-num" }, [
        el("b", { text: valueText }),
        pct ? el("span", { class: "muted", text: ` ${pct}` }) : null,
      ]),
    ]),
    el("div", { class: "meter", "aria-hidden": "true" }, [gauge]),
  ]);
}

export function renderReasons(root, overview) {
  const { rewrites, fallback_reasons: reasons } = overview;
  if (rewrites.total === 0) {
    fill(root, empty(`${NO_DATA} — 이 기간에 요청이 없습니다.`));
    return;
  }
  if (rewrites.fallback === 0) {
    fill(
      root,
      el("p", { class: "all-good" }, [
        tag("ok", "원문 유지 없음"),
        ` 이 기간의 요청 ${num(rewrites.total)}건은 모두 검증을 통과했습니다.`,
      ]),
    );
    return;
  }
  fill(
    root,
    el(
      "ul",
      { class: "meters" },
      reasons.map((r) =>
        meterItem(label(CHECK_LABEL, r.check), `${num(r.count)}건`, share(r.count, rewrites.fallback)),
      ),
    ),
    hint(
      `원문을 유지한 ${num(rewrites.fallback)}건의 마지막 검증 결과입니다. ` +
        "한 요청에 사유가 둘이면 양쪽에 셉니다.",
    ),
  );
}

function ordered(counts, known) {
  const extra = Object.keys(counts).filter((k) => !known.includes(k));
  return [...known.filter((k) => k in counts), ...extra];
}

export function renderMix(root, overview) {
  const { rewrites } = overview;
  if (rewrites.total === 0) {
    fill(root, empty(`${NO_DATA} — 이 기간에 요청이 없습니다.`));
    return;
  }
  const group = (title, counts, known, names) =>
    el("div", { class: "mix-group" }, [
      el("h3", { text: title }),
      el(
        "ul",
        { class: "meters" },
        ordered(counts, known).map((key) =>
          meterItem(label(names, key), `${num(counts[key])}건`, share(counts[key], rewrites.total)),
        ),
      ),
    ]);
  fill(
    root,
    el("div", { class: "mix" }, [
      group("조건", rewrites.by_condition, ["A", "B", "C", "D"], CONDITION_LABEL),
      group("채널", rewrites.by_channel, ["web", "mobile", "desktop", "api"], CHANNEL_LABEL),
    ]),
  );
}

// ---------- 비용·사용량, 수집 상태 ----------

function row(name, ...values) {
  return el("div", { class: "row" }, [el("dt", { text: name }), el("dd", {}, values)]);
}

function money(value) {
  const text = usd(value);
  return text ? el("b", { text }) : tag("off", "단가 미설정");
}

export function renderCost(root, overview) {
  const { tokens, graph, limits } = overview;
  const rewriteCost = tokens.estimated_cost_usd;
  const graphCost = graph.estimated_cost_usd;
  const graphUsed = graph.enabled || graph.ingested_messages > 0;
  const globalLimits = [];
  if (limits.global_daily_rewrite_limit > 0) globalLimits.push(`${num(limits.global_daily_rewrite_limit)}회`);
  if (limits.global_daily_token_limit > 0) globalLimits.push(`토큰 ${num(limits.global_daily_token_limit)}`);

  const rows = el("dl", { class: "rows" }, [
    row(
      "첨삭 (모델 호출)",
      el("span", { class: "muted", text: `입력 ${num(tokens.input)} · 출력 ${num(tokens.output)} 토큰 ` }),
      money(rewriteCost),
    ),
    graphUsed
      ? row(
          "사건 정리 (그래프 수집)",
          el("span", { class: "muted", text: `메시지 ${num(graph.ingested_messages)}건 ` }),
          money(graphCost),
        )
      : row("사건 정리 (그래프 수집)", tag("off", "사용 안 함")),
    row(
      "하루 한도에 닿은 사용자",
      el("b", { text: `${num(limits.users_at_daily_limit)}명` }),
      el("span", {
        class: "muted",
        text: ` (한도: 하루 ${num(limits.daily_rewrite_limit)}회 · 토큰 ${num(limits.daily_token_limit)})`,
      }),
    ),
    row(
      "오늘 전체 사용량",
      el("b", { text: `첨삭 ${num(limits.today_rewrites)}회 · 토큰 ${num(limits.today_tokens)}` }),
      el("span", {
        class: "muted",
        text: globalLimits.length ? ` (전체 한도: ${globalLimits.join(" · ")})` : " (전체 한도 꺼짐)",
      }),
    ),
  ]);

  const nodes = [rows];
  if (rewriteCost !== null && graphCost !== null && rewriteCost + graphCost > 0) {
    const sum = rewriteCost + graphCost;
    nodes.push(
      el("ul", { class: "meters" }, [
        meterItem("첨삭", usd(rewriteCost), share(rewriteCost, sum)),
        meterItem("사건 정리", usd(graphCost), share(graphCost, sum)),
      ]),
    );
  }
  const notes = [];
  if (rewriteCost === null || (graphUsed && graphCost === null)) {
    notes.push(
      "단가를 설정하면 금액으로 환산합니다 (COST_REWRITE_INPUT_PER_MTOK 등). 설정 전에는 금액을 만들지 않습니다.",
    );
  }
  if (rewriteCost !== null || graphCost !== null) {
    notes.push("금액은 설정한 단가로 환산한 추정치입니다. 실제 청구액은 Claude Console에서 확인하세요.");
  }
  if (tokens.cache_read + tokens.cache_creation > 0) {
    notes.push(
      `캐시 토큰(읽기 ${num(tokens.cache_read)} · 쓰기 ${num(tokens.cache_creation)})은 환산에 넣지 않았습니다.`,
    );
  }
  nodes.push(hint(notes.join(" ")));
  fill(root, ...nodes);
}

/** `actions.onRetryHeld(건수, 버튼)`을 주면 보류가 있을 때 보류 줄에 "다시 시도" 버튼을 낸다. */
export function renderIngest(root, overview, actions = {}) {
  const { graph, health } = overview;
  const { jobs } = graph;
  const waiting = !graph.enabled
    ? tag("off", "수집 꺼짐")
    : graph.pending_messages > 0
      ? tag("warn", "대기 중")
      : tag("ok", "없음");
  const db = healthView(health.postgres);
  const graphDb = healthView(health.neo4j);
  const held = heldView(graph);
  let retry = null;
  if (held.canRetry && typeof actions.onRetryHeld === "function") {
    retry = el("button", { type: "button", class: "btn ghost row-action", text: "다시 시도" });
    retry.addEventListener("click", () => actions.onRetryHeld(held.count, retry));
  }
  fill(
    root,
    el("dl", { class: "rows" }, [
      row("사건 정리를 기다리는 메시지", el("b", { text: `${num(graph.pending_messages)}건 ` }), waiting),
      row(
        "거듭 실패해 보류된 메시지",
        el("b", { text: `${num(held.count)}건 ` }),
        tag(held.tag.kind, held.tag.text),
        retry,
      ),
      row(
        "대기·진행 중인 작업",
        el("b", { text: `대기 ${num(jobs.pending)}건 · 진행 ${num(jobs.running)}건` }),
      ),
      row(
        "실패한 작업",
        el("b", { text: `${num(jobs.failed)}건 ` }),
        jobs.failed > 0 ? tag("bad", "확인 필요") : tag("ok", "없음"),
      ),
      row(
        "상한에 닿아 멈춘 작업",
        el("b", { text: `${num(jobs.limited)}건 ` }),
        jobs.limited > 0 ? tag("warn", "주의") : tag("ok", "없음"),
      ),
      row("데이터베이스 (PostgreSQL)", tag(db.kind, db.text)),
      row("그래프 DB (Neo4j)", tag(graphDb.kind, graphDb.text)),
    ]),
    hint(
      "작업 수와 대기·보류 메시지는 기간과 상관없이 지금 상태입니다. 다시 시도는 보류만 풀어 다시 넣습니다(이미 정리된 내용은 그대로) — 메시지마다 모델 호출 비용이 듭니다.",
    ),
  );
}

export function renderOverview(roots, overview, actions = {}) {
  renderKpis(roots.kpis, overview);
  renderDaily(roots.daily, overview);
  renderReasons(roots.reasons, overview);
  renderMix(roots.mix, overview);
  renderCost(roots.cost, overview);
  renderIngest(roots.ingest, overview, actions);
}

// ---------- 표 ----------

function table(caption, columns, rows) {
  const head = el(
    "tr",
    {},
    columns.map((c) => el("th", { scope: "col", class: c.num ? "num" : null, text: c.title })),
  );
  const body = rows.map((item) =>
    el(
      "tr",
      {},
      columns.map((c) => el("td", { class: c.num ? "num" : null }, [c.cell(item)])),
    ),
  );
  // 좁은 화면에서는 가로로 스크롤된다 → 키보드로도 스크롤할 수 있게 포커스를 받는다.
  return el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": caption }, [
    el("table", {}, [
      el("caption", { class: "sr-only", text: caption }),
      el("thead", {}, [head]),
      el("tbody", {}, body),
    ]),
  ]);
}

export function renderFailure(root, message) {
  fill(root, el("p", { class: "empty error", text: `불러오지 못했습니다. ${message}` }));
}

export function renderFeedback(root, items) {
  if (!items.length) {
    fill(root, empty("거절 피드백이 없습니다."));
    return;
  }
  fill(
    root,
    table(
      "최근 거절 피드백",
      [
        { title: "시각", cell: (f) => timeCell(f.created_at) },
        { title: "사용자", cell: (f) => idCell(f.user_id) },
        { title: "관계", cell: (f) => label(RELATION_LABEL, f.relationship_type) },
        { title: "채널", cell: (f) => label(CHANNEL_LABEL, f.channel) },
        { title: "조건", cell: (f) => f.condition },
        { title: "사유 (선택형)", cell: (f) => label(REASON_LABEL, f.reason_code ?? "unspecified") },
      ],
      items,
    ),
    hint("사용자가 직접 적은 사유와 고쳐 쓴 문장은 이 화면에 나오지 않습니다."),
  );
}

function importStatus(job) {
  if (job.status === "failed") {
    return tag("bad", job.error_code ? `실패 (${job.error_code})` : "실패");
  }
  return job.status === "completed" ? tag("ok", "완료") : label(JOB_STATUS_LABEL, job.status);
}

const GRAPH_TAG = { completed: "ok", failed: "bad", limited: "warn", skipped: "off" };

function graphStatus(job) {
  const text = label(GRAPH_STATUS_LABEL, job.graph_status);
  const kind = GRAPH_TAG[job.graph_status];
  return kind ? tag(kind, text) : el("span", { class: "tag", text });
}

export function renderJobs(root, items) {
  if (!items.length) {
    fill(root, empty("가져오기 작업이 없습니다."));
    return;
  }
  fill(
    root,
    table(
      "최근 가져오기 작업",
      [
        { title: "시각", cell: (j) => timeCell(j.created_at) },
        { title: "사용자", cell: (j) => idCell(j.user_id) },
        { title: "가져오기", cell: importStatus },
        { title: "메시지", num: true, cell: (j) => `${num(j.imported)}건` },
        { title: "문체 묶음", num: true, cell: (j) => `${num(j.episodes)}개` },
        { title: "사건 정리", cell: graphStatus },
        { title: "정리한 메시지", num: true, cell: (j) => `${num(j.graph_episodes)}건` },
        { title: "오류 종류", cell: (j) => j.graph_error ?? "–" },
      ],
      items,
    ),
    hint("오류는 종류만 보여 줍니다. 자세한 내용은 서버 로그에서 확인하세요."),
  );
}

export function renderUsers(root, page) {
  if (!page.items.length) {
    fill(root, empty(page.total > 0 ? "이 쪽에는 사용자가 없습니다." : "가입한 사용자가 없습니다."));
    return;
  }
  fill(
    root,
    table(
      "사용자",
      [
        { title: "사용자", cell: (u) => idCell(u.user_id) },
        { title: "이름", cell: (u) => u.username },
        { title: "가입", cell: (u) => timeCell(u.created_at) },
        { title: "동의한 안내 버전", cell: (u) => u.consent_version ?? "–" },
        { title: "상대", num: true, cell: (u) => `${num(u.contacts)}명` },
        { title: "메시지", num: true, cell: (u) => `${num(u.messages)}건` },
        { title: "첨삭", num: true, cell: (u) => `${num(u.rewrites)}건` },
        { title: "마지막 첨삭", cell: (u) => timeCell(u.last_rewrite_at) },
      ],
      page.items,
    ),
  );
}

export function renderAccess(root, items) {
  if (!items.length) {
    fill(root, empty("접근 기록이 없습니다."));
    return;
  }
  fill(
    root,
    table(
      "관리자 접근 기록",
      [
        { title: "시각", cell: (g) => timeCell(g.newest) },
        { title: "관리자", cell: (g) => g.admin_username },
        { title: "본 것·한 일", cell: (g) => g.actions.map((a) => label(ACTION_LABEL, a)).join(" · ") },
        { title: "호출", num: true, cell: (g) => `${num(g.count)}회` },
      ],
      groupAccess(items),
    ),
    hint("같은 관리자가 1분 안에 이어서 본 것은 한 줄로 묶었습니다. 이 화면을 여는 것도 기록됩니다."),
  );
}
