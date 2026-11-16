// 첨삭 결과를 그리는 조각(첨삭 화면과 이력 상세가 함께 쓴다).
// 읽는 순서: 추천 문장 → 지킨 항목 → 주의 → (행동) → 바꾼 이유·참고한 표현·부가 정보.
// 모델 출력은 전부 글자(text node)로 넣는다.

import { api } from "../api.js";
import { formatDateTime, formatNumber } from "../lib/format.js";
import { evidenceNotes, fallbackView, markSegments, metaLine, preservedChips, resultMeta } from "../lib/model.js";
import { h, uid } from "./dom.js";
import { loadInto, note } from "./parts.js";

/** 추천 문장. 초안의 날짜·숫자 등이 그대로 남은 자리를 <mark>로 강조한다. */
export function sentenceNode(result, attrs = {}) {
  const segments = markSegments(result.result_text, result.status === "recommended" ? result.preserved_items : []);
  return h(
    "p",
    { class: "result-text", lang: "ko", ...attrs },
    segments.map((seg) => (seg.mark ? h("mark", { title: "초안의 표현이 그대로 남았어요" }, seg.text) : seg.text)),
  );
}

/** 지킨 항목 칩 줄(없으면 null). */
export function keptNode(result) {
  const chips = preservedChips(result);
  if (!chips.length) return null;
  const labelId = uid("kept");
  return h(
    "div",
    { class: "kept" },
    h("span", { class: "kept-label", id: labelId }, "지킨 항목"),
    h(
      "ul",
      { class: "kept-list", "aria-labelledby": labelId },
      chips.map((chip) =>
        h("li", { class: "tag tag-ok" }, h("span", { "aria-hidden": "true" }, "✓ "), h("span", { class: "sr-only" }, "지킴: "), chip.label),
      ),
    ),
  );
}

/** 주의 블록 — 모델이 남긴 확인할 점. 본문에 넣지 않고 여기서만 보여 준다. */
export function warningsNode(result) {
  const warnings = (result.warnings || []).filter(Boolean);
  if (!warnings.length) return null;
  return h(
    "div",
    { class: "warnbox", role: "note" },
    h("p", { class: "warnbox-title" }, h("span", { "aria-hidden": "true" }, "! "), "확인해 주세요"),
    h(
      "ul",
      null,
      warnings.map((w) => h("li", null, w)),
    ),
  );
}

/** 원문 유지 안내: 왜 추천하지 않았는지와 지키지 못한 항목. history=true면 이력 화면용 문구. */
export function fallbackNode(result, { history = false } = {}) {
  const view = fallbackView(result, { history });
  return h(
    "div",
    { class: "fallback" },
    h("p", null, h("span", { class: "tag tag-warn" }, "초안을 그대로 뒀어요")),
    h("p", { class: "fallback-title" }, view.headline),
    h("p", { class: "fallback-lead" }, view.lead),
    view.items.length
      ? h(
          "ul",
          { class: "fallback-items", "aria-label": "지키지 못한 항목" },
          view.items.map((item) => h("li", null, item)),
        )
      : null,
  );
}

function reasonsNode(result) {
  const reasons = (result.change_reasons || []).filter(Boolean);
  if (!reasons.length) return null;
  return h(
    "div",
    { class: "extras-block" },
    h("h3", { class: "extras-title" }, "바꾼 이유"),
    h(
      "ul",
      { class: "plain-list" },
      reasons.map((r) => h("li", null, r)),
    ),
  );
}

const ROLE_LABEL = { me: "나", contact: "상대", graph: "정리된 사실" };

function renderEvidence(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  const blocks = items.map((item) =>
    h(
      "li",
      { class: "evidence-item" },
      h(
        "ul",
        { class: "evidence-messages" },
        (item.messages || []).map((m) =>
          h(
            "li",
            { class: m.speaker_role === "me" ? "is-me" : "" },
            h("span", { class: "evidence-who" }, ROLE_LABEL[m.speaker_role] || m.speaker_role),
            h("span", { class: "evidence-when" }, formatDateTime(m.sent_at)),
            h("span", { class: "evidence-text" }, m.text),
          ),
        ),
      ),
    ),
  );
  // 원문을 못 보여 주는 까닭은 단정하지 않는다(지워졌을 수도, 근거 기록이 없던 때의 결과일 수도 있다)
  const notes = evidenceNotes(data, blocks.length);
  return [
    blocks.length ? h("ul", { class: "evidence-list" }, blocks) : null,
    notes.empty ? note(notes.empty) : null,
    notes.missing ? note(notes.missing) : null,
  ];
}

/** 참고한 내 과거 표현 — 펼칠 때 서버에서 지금 남아 있는 원문을 읽어 온다. */
function evidenceNode(rec) {
  const ids = rec.result.evidence_ids || [];
  if (!ids.length) return null;
  const body = h("div", { class: "evidence-body" });
  let loaded = false;
  const details = h(
    "details",
    {
      class: "fold",
      onToggle: () => {
        if (!details.open || loaded) return;
        loaded = true;
        loadInto(body, {
          lines: 2,
          load: () => api.rewrites.evidence(rec.id),
          render: renderEvidence,
          onUnavailable: () =>
            note(`원문은 지금 불러올 수 없어요. 참고한 표현 번호: ${ids.join(", ")}`),
        });
      },
    },
    h("summary", null, `참고한 내 과거 표현 ${ids.length}개`),
    body,
  );
  return details;
}

function technicalNode(meta) {
  const rows = [
    ["모델", meta.model],
    ["프롬프트", meta.prompt],
    ["검색 방식", meta.retrieval],
    ["토큰", meta.tokensIn != null ? `입력 ${formatNumber(meta.tokensIn)} · 출력 ${formatNumber(meta.tokensOut ?? 0)}` : ""],
    ["요청 경로", meta.channel || ""],
  ].filter(([, v]) => v);
  const attempts = meta.attemptLog.length
    ? h(
        "ol",
        { class: "attempt-log" },
        meta.attemptLog.map((a) =>
          h(
            "li",
            null,
            h("span", { class: a.passed ? "tag tag-ok" : "tag tag-warn" }, a.passed ? "검증 통과" : "검증에 걸림"),
            a.violations.length ? h("span", { class: "attempt-why" }, ` ${a.violations.join(" / ")}`) : null,
          ),
        ),
      )
    : null;
  return h(
    "details",
    { class: "fold" },
    h("summary", null, "만든 과정 보기"),
    h(
      "dl",
      { class: "meta-list" },
      rows.map(([k, v]) => [h("dt", null, k), h("dd", null, v)]),
    ),
    attempts ? h("div", null, h("p", { class: "extras-title" }, "시도 기록"), attempts) : null,
  );
}

/**
 * 결과 아래쪽: 바꾼 이유, 참고한 내 과거 표현, 부가 정보(적용 조건·걸린 시간·시도 횟수).
 * requested는 화면에서 고른 조건("auto"면 적용 조건 옆에 '자동'을 붙인다). 이력에서는 null.
 */
export function extrasNode(rec, requested = null) {
  const meta = resultMeta(rec, requested);
  const evidence = evidenceNode(rec);
  return h(
    "div",
    { class: "result-extras" },
    reasonsNode(rec.result),
    evidence,
    h(
      "div",
      { class: "extras-block" },
      h("p", { class: "meta-line" }, metaLine(meta), evidence ? null : " · 참고한 과거 표현 없음"),
      technicalNode(meta),
    ),
  );
}
