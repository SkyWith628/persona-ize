// 첨삭 화면(#/compose) — 기본 화면이자 가장 중요한 화면.
// 상대 고르기 → 초안 → 상황 → "말투 맞추기" → 결과(추천 문장 → 지킨 항목 → 주의 → 행동).
// 자동 전송은 없다: 복사까지만 하고 전송은 사용자가 직접 한다.

import { api } from "../api.js";
import { isAbort } from "../lib/errors.js";
import { countCodePoints, formatNumber, initial, isMacLike, uuid4, withJosa } from "../lib/format.js";
import {
  ADJUSTS,
  CONDITIONS,
  MAX_DRAFT_CHARS,
  MAX_SITUATION_CHARS,
  REASON_CODES,
  SITUATION_CHIPS,
  adjustLabel,
  buildSituation,
  pushRecent,
  reasonLabel,
  recentContacts,
  relLabel,
  rewriteErrorView,
} from "../lib/model.js";
import { buildHash } from "../lib/router.js";
import { loadContacts, refreshUsage } from "../session.js";
import { recentContactIds, saveRecentContactIds, store } from "../store.js";
import { copyText, selectContents } from "../ui/clipboard.js";
import { contactPicker } from "../ui/combobox.js";
import { addContactDialog } from "../ui/contact-dialog.js";
import { dialogOpen } from "../ui/dialog.js";
import { h, isTypingTarget, setChildren, uid } from "../ui/dom.js";
import { emptyState, errorState, kbd, pageHead, skeleton, tag } from "../ui/parts.js";
import { extrasNode, fallbackNode, keptNode, sentenceNode, warningsNode } from "../ui/result-card.js";
import { toast } from "../ui/toast.js";

// 화면을 떠났다 돌아와도 쓰던 초안과 결과가 남도록 모듈 변수에 둔다.
// 브라우저 저장소에는 쓰지 않는다 — 초안은 이 탭의 메모리에만 있다.
const fresh = () => ({
  contactId: null,
  chipIds: null,
  draft: "",
  chip: null,
  situation: "",
  condition: "auto",
  advancedOpen: false,
  stage: "idle", // idle | loading | result | fallback | error
  rec: null,
  sentDraft: "",
  sentCondition: "auto",
  askedAdjust: null,
  error: null,
  notice: null,
  editing: false,
  feedback: null, // { accepted, edited, reasonCode, saved }
  rejectOpen: false,
});
let mem = fresh();
store.on("user-changed", () => {
  mem = fresh();
});

/** 다른 화면에서 "이 상대로 첨삭하기"를 눌렀을 때 미리 골라 둔다. */
export function presetContact(contactId) {
  if (mem.contactId !== contactId) {
    Object.assign(mem, { contactId, stage: "idle", rec: null, error: null, feedback: null, editing: false, notice: null });
  }
}

export function mountCompose(root, ctx) {
  let seq = 0; // 요청 번호 — 늦게 온 응답을 버리는 데 쓴다
  let controller = null;
  let destroyed = false;
  let editEl = null;
  const modKey = isMacLike(navigator.platform || navigator.userAgent) ? "⌘" : "Ctrl";

  if (ctx.route.query.contact) presetContact(ctx.route.query.contact);

  // ---------- 뼈대 ----------
  const pickerBox = h("div", { class: "picker" });
  const pickerError = h("p", { class: "field-error", role: "alert", hidden: true });
  const draftId = uid("draft");
  const draftHintId = uid("draft-hint");
  const counterEl = h("span", { class: "counter", id: uid("draft-count") });
  const draftError = h("p", { class: "field-error", role: "alert", hidden: true });
  const draftEl = h("textarea", {
    id: draftId,
    class: "textarea draft",
    rows: 7,
    spellcheck: "false",
    placeholder: "예) 이번 주 금요일은 어렵고 다음 주 수요일까지는 가능합니다.",
    "aria-describedby": `${draftHintId} ${counterEl.id}`,
    value: mem.draft,
  });
  const situationId = uid("situation");
  const situationEl = h("input", {
    id: situationId,
    class: "input",
    type: "text",
    maxlength: MAX_SITUATION_CHARS,
    placeholder: "직접 쓰기 (예: 일정이 겹쳐서 미뤄야 함)",
    "aria-label": "상황 직접 쓰기",
    value: mem.situation,
  });
  const chipsEl = h("div", { class: "chip-row", role: "group", "aria-label": "상황 고르기" });
  const runBtn = h("button", { type: "submit", class: "btn btn-primary btn-lg" });
  const conditionTag = h("span", { class: "tag" });
  const advancedEl = h("details", { class: "advanced" });
  const outEl = h("section", { class: "compose-output", "aria-label": "첨삭 결과" });
  const liveEl = h("p", { class: "sr-only", role: "status" });

  const form = h(
    "form",
    {
      class: "card compose-input",
      novalidate: true,
      onSubmit: (e) => {
        e.preventDefault();
        run();
      },
    },
    h(
      "fieldset",
      { class: "field" },
      h("legend", null, "누구에게 보내나요?"),
      pickerBox,
      pickerError,
    ),
    h(
      "div",
      { class: "field" },
      h("div", { class: "field-label-row" }, h("label", { for: draftId }, "초안"), counterEl),
      draftEl,
      h("p", { class: "hint", id: draftHintId }, "쓰고 싶은 말을 편하게 적어 주세요. 날짜·숫자·약속은 그대로 지켜요."),
      draftError,
    ),
    h(
      "fieldset",
      { class: "field" },
      h("legend", null, "상황 ", h("span", { class: "optional" }, "선택")),
      chipsEl,
      situationEl,
    ),
    h("div", { class: "form-actions" }, runBtn),
    advancedEl,
  );

  root.replaceChildren(
    pageHead("첨삭", "초안을 쓰면 상대에 맞는 내 말투로 다듬어요. 사실은 그대로 두고, 보내는 건 직접 해요."),
    h("div", { class: "compose-grid" }, form, outEl),
    liveEl,
  );

  const contacts = () => store.contacts || [];
  const currentContact = () => contacts().find((c) => c.id === mem.contactId) || null;
  const announce = (message) => {
    liveEl.textContent = "";
    liveEl.textContent = message;
  };

  // ---------- 상대 고르기 ----------
  let picker = null;

  function selectContact(contactId, { focusDraft = true } = {}) {
    showFieldError(pickerError, "");
    if (mem.contactId !== contactId) {
      cancelInFlight();
      Object.assign(mem, {
        contactId,
        stage: "idle",
        rec: null,
        error: null,
        notice: null,
        feedback: null,
        editing: false,
        rejectOpen: false,
      });
      renderOutput();
    }
    saveRecentContactIds(pushRecent(recentContactIds(), contactId));
    renderPicker();
    if (focusDraft) draftEl.focus();
  }

  async function createContact(typed = "") {
    const contact = await addContactDialog({ alias: typed });
    if (!contact || destroyed) return;
    if (contact.persona_version == null) {
      toast(`${withJosa(contact.alias, "을/를")} 추가했어요. 말투 설정은 '상대'에서 확인하고 저장해 주세요.`, { kind: "info" });
    } else {
      toast(`${withJosa(contact.alias, "을/를")} 추가했어요. 관계에 맞는 기본 말투로 시작해요.`, { kind: "ok" });
    }
    selectContact(contact.id);
  }

  function renderPicker() {
    const list = contacts();
    if (!list.length) {
      picker = null;
      setChildren(
        pickerBox,
        emptyState(
          "아직 상대가 없어요",
          "누구에게 보낼지 먼저 추가해 주세요. 실명 대신 별칭을 써도 돼요.",
          h("button", { type: "button", class: "btn", onClick: () => createContact() }, "상대 추가"),
        ),
      );
      return;
    }
    const chips = recentContacts(list, mem.chipIds || recentContactIds(), mem.contactId);
    mem.chipIds = chips.map((c) => c.id);
    picker = contactPicker({
      getContacts: contacts,
      onSelect: (c) => selectContact(c.id),
      onCreate: (typed) => createContact(typed),
      label: "다른 상대 찾기",
      placeholder: list.length > chips.length ? `다른 상대 찾기 (전체 ${list.length}명)` : "이름으로 찾기",
    });
    const selected = currentContact();
    setChildren(
      pickerBox,
      h(
        "div",
        { class: "chip-row", role: "group", "aria-label": "최근 고른 상대" },
        chips.map((c) =>
          h(
            "button",
            {
              type: "button",
              class: "chip chip-contact",
              "aria-pressed": String(c.id === mem.contactId),
              onClick: () => selectContact(c.id),
            },
            h("span", { class: "avatar", "aria-hidden": "true" }, initial(c.alias)),
            h("span", { class: "chip-name" }, c.alias),
            h("span", { class: "chip-sub" }, relLabel(c.relationship_type)),
          ),
        ),
      ),
      picker.el,
      selected
        ? h(
            "p",
            { class: "picker-selected" },
            h("strong", null, selected.alias),
            `에게 보내요 · ${relLabel(selected.relationship_type)} · `,
            selected.persona_version == null
              ? [tag("말투 설정 없음", "warn"), " ", h("a", { href: buildHash("contact", { id: selected.id }) }, "설정하기")]
              : [`말투 설정 v${selected.persona_version} `, h("a", { href: buildHash("contact", { id: selected.id }) }, "보기·고치기")],
          )
        : h("p", { class: "picker-selected muted" }, "위에서 상대를 골라 주세요."),
    );
  }

  // ---------- 초안·상황·조건 ----------
  function showFieldError(el, message) {
    el.textContent = message || "";
    el.hidden = !message;
  }

  function updateCounter() {
    const n = countCodePoints(draftEl.value.trim());
    const over = n > MAX_DRAFT_CHARS;
    counterEl.textContent = over
      ? `${formatNumber(n)} / ${formatNumber(MAX_DRAFT_CHARS)}자 — ${formatNumber(n - MAX_DRAFT_CHARS)}자 넘었어요`
      : `${formatNumber(n)} / ${formatNumber(MAX_DRAFT_CHARS)}자`;
    counterEl.classList.toggle("is-over", over);
    draftEl.setAttribute("aria-invalid", String(over));
    return over;
  }

  function renderSituationChips() {
    setChildren(
      chipsEl,
      SITUATION_CHIPS.map((label) =>
        h(
          "button",
          {
            type: "button",
            class: "chip",
            "aria-pressed": String(mem.chip === label),
            onClick: () => {
              mem.chip = mem.chip === label ? null : label;
              renderSituationChips();
            },
          },
          label,
        ),
      ),
    );
  }

  function renderRunButton(loading = false) {
    // disabled로 두면 버튼에 있던 포커스가 사라진다 → aria-disabled로 표시하고, 다시 눌러도 run()이 무시한다
    if (loading) {
      runBtn.setAttribute("aria-disabled", "true");
      runBtn.setAttribute("aria-busy", "true");
    } else {
      runBtn.removeAttribute("aria-disabled");
      runBtn.removeAttribute("aria-busy");
    }
    setChildren(runBtn, loading ? "만드는 중…" : ["말투 맞추기 ", kbd(modKey, "Enter")]);
  }

  function renderAdvanced() {
    const name = uid("cond");
    const current = CONDITIONS.find((c) => c.value === mem.condition) || CONDITIONS[0];
    conditionTag.textContent = current.label;
    advancedEl.open = mem.advancedOpen;
    setChildren(
      advancedEl,
      h("summary", null, "고급 · 실험 조건 ", conditionTag),
      h(
        "fieldset",
        { class: "radio-list" },
        h("legend", { class: "sr-only" }, "실험 조건"),
        CONDITIONS.map((c) => {
          const inputId = uid("cond-opt");
          return h(
            "div",
            { class: "radio-row" },
            h("input", {
              type: "radio",
              id: inputId,
              name,
              value: c.value,
              checked: c.value === mem.condition,
              onChange: () => {
                mem.condition = c.value;
                conditionTag.textContent = c.label;
              },
            }),
            h("label", { for: inputId }, h("span", { class: "radio-title" }, c.label), h("span", { class: "radio-desc" }, c.desc)),
          );
        }),
      ),
      h("p", { class: "hint" }, "평소에는 자동으로 두면 돼요. A~D는 방법별 결과를 비교해 볼 때 써요."),
    );
  }

  advancedEl.addEventListener("toggle", () => {
    mem.advancedOpen = advancedEl.open;
  });

  draftEl.addEventListener("input", () => {
    mem.draft = draftEl.value;
    showFieldError(draftError, "");
    updateCounter();
    if (mem.stage === "loading") {
      // 요청 중에 초안이 바뀌면 그 결과는 지금 초안의 것이 아니다 → 적용하지 않는다
      cancelInFlight();
      mem.stage = "idle";
      mem.notice = "초안을 고쳐서 방금 요청은 취소했어요. 다 쓴 뒤 다시 '말투 맞추기'를 눌러 주세요.";
      renderOutput();
    } else {
      updateStaleNote();
    }
  });
  situationEl.addEventListener("input", () => {
    mem.situation = situationEl.value;
  });

  // ---------- 요청 ----------
  function cancelInFlight() {
    seq += 1;
    if (controller) controller.abort();
    controller = null;
    renderRunButton(false);
  }

  async function run({ adjust = null } = {}) {
    if (mem.stage === "loading" || destroyed) return;
    const contact = currentContact();
    showFieldError(pickerError, "");
    showFieldError(draftError, "");
    if (!contact) {
      showFieldError(pickerError, "누구에게 보낼지 먼저 골라 주세요.");
      if (picker) picker.input.focus();
      return;
    }
    const draft = draftEl.value.trim();
    if (!draft) {
      showFieldError(draftError, "초안을 먼저 써 주세요.");
      draftEl.focus();
      return;
    }
    if (updateCounter()) {
      showFieldError(draftError, `초안은 ${formatNumber(MAX_DRAFT_CHARS)}자까지 보낼 수 있어요. 조금 줄여 주세요.`);
      draftEl.focus();
      return;
    }

    const condition = mem.condition;
    const sentDraft = draftEl.value;
    seq += 1;
    const mine = seq;
    if (controller) controller.abort();
    controller = new AbortController();
    Object.assign(mem, { stage: "loading", error: null, notice: null, editing: false, rejectOpen: false, feedback: null });
    renderRunButton(true);
    renderOutput();
    announce(`${contact.alias}에게 맞게 바꾸는 중이에요.`);

    const body = {
      contact_id: contact.id,
      draft_text: draft,
      situation: buildSituation(mem.chip, situationEl.value),
      condition,
      channel: "web",
    };
    if (adjust) body.adjust = adjust;

    try {
      // 요청마다 새 Idempotency-Key — "다시 만들기"는 같은 본문이라도 새 결과를 받아야 한다
      const rec = await api.rewrites.create(body, uuid4(), controller.signal);
      if (mine !== seq || destroyed) return; // 그 사이 상대나 초안이 바뀌었다 — 늦게 온 결과는 버린다
      Object.assign(mem, {
        rec,
        sentDraft,
        sentCondition: condition,
        askedAdjust: adjust,
        stage: rec.result.status === "recommended" ? "result" : "fallback",
      });
      announce(
        mem.stage === "result"
          ? `추천 문장이 준비됐어요. ${rec.result.result_text}`
          : "추천하지 않고 초안을 그대로 뒀어요.",
      );
    } catch (err) {
      if (isAbort(err) || mine !== seq || destroyed) return;
      if (err.status === 401) return; // 로그인 화면으로 넘어간다(초안은 메모리에 남는다)
      mem.error = rewriteErrorView(err, { contact, condition });
      mem.stage = "error";
      if (mem.error.action === "advanced") mem.advancedOpen = true;
      announce(`${mem.error.title}. ${mem.error.body}`);
    }
    controller = null;
    renderRunButton(false);
    if (mem.advancedOpen !== advancedEl.open) advancedEl.open = mem.advancedOpen;
    renderOutput();
    focusOutput();
    refreshUsage();
  }

  /**
   * 결과가 오면 주 행동에 포커스를 둔다(Enter=복사).
   * 그 사이 사용자가 다른 입력칸(상황 직접 쓰기, 상대 찾기)이나 이 화면 밖으로 옮겨 갔으면 건드리지 않는다.
   */
  function focusOutput() {
    const active = document.activeElement;
    const inForm = active instanceof Node && form.contains(active);
    const typingElsewhere = inForm && active !== draftEl && isTypingTarget(active);
    const mayMove = !active || active === document.body || outEl.contains(active) || (inForm && !typingElsewhere);
    if (!mayMove) return;
    const target =
      outEl.querySelector("[data-act='copy']") ||
      outEl.querySelector("[data-act='retry']") ||
      outEl.querySelector("[data-act='goto']") ||
      outEl.querySelector("[data-act='close']");
    if (target) target.focus();
    outEl.scrollIntoView({ block: "nearest" });
  }

  // ---------- 결과 행동 ----------
  async function copy() {
    if (mem.stage !== "result" || !mem.rec) return;
    const rec = mem.rec;
    const original = rec.result.result_text;
    const text = mem.editing && editEl ? editEl.value : original;
    if (!text.trim()) {
      toast("복사할 문장이 비어 있어요.", { kind: "warn" });
      return;
    }
    const copied = await copyText(text); // 클립보드를 먼저 — 사용자의 누름 안에서 해야 한다
    const edited = text !== original ? text : null;
    let saved = true;
    try {
      // 복사에 실패해도 피드백은 저장한다
      await api.rewrites.feedback(rec.id, { accepted: true, edited_text: edited });
    } catch (err) {
      saved = false;
      if (err.status === 401) return;
    }
    if (copied) toast("복사했어요. 메신저에 붙여 넣고, 전송은 직접 하세요.", { kind: "ok" });
    else toast("복사하지 못했어요. 문장을 선택해 뒀으니 직접 복사해 주세요.", { kind: "warn" });
    if (!saved) toast("적용 기록은 저장하지 못했어요. 문장은 그대로 쓸 수 있어요.", { kind: "warn" });
    if (mem.rec !== rec || destroyed) return;
    mem.feedback = { accepted: true, edited: Boolean(edited), saved, copied };
    renderFeedbackStatus();
    announce(copied ? "복사했어요." : "복사하지 못했어요.");
    if (!copied) selectContents(mem.editing && editEl ? editEl : outEl.querySelector(".result-text"));
  }

  async function reject(code) {
    if (!mem.rec) return;
    const rec = mem.rec;
    try {
      await api.rewrites.feedback(rec.id, { accepted: false, reason_code: code });
    } catch (err) {
      if (err.status !== 401) toast(err.message || "기록하지 못했어요.", { kind: "error" });
      return;
    }
    if (mem.rec !== rec || destroyed) return;
    mem.feedback = { accepted: false, reasonCode: code, saved: true };
    mem.rejectOpen = false;
    toast("알려 줘서 고마워요. 나답지 않다고 기록했어요.", { kind: "ok" });
    renderOutput();
    outEl.querySelector("[data-act='reject']")?.focus();
  }

  function closeResult() {
    cancelInFlight();
    Object.assign(mem, { stage: "idle", rec: null, error: null, notice: null, editing: false, rejectOpen: false, feedback: null });
    renderOutput();
    draftEl.focus();
  }

  function toggleEdit(on) {
    mem.editing = on;
    renderOutput();
    if (on && editEl) {
      editEl.focus();
      editEl.setSelectionRange(editEl.value.length, editEl.value.length);
    } else {
      outEl.querySelector("[data-act='copy']")?.focus();
    }
  }

  let staleEl = null;
  function updateStaleNote() {
    if (!staleEl) return;
    staleEl.hidden = !(mem.stage === "result" || mem.stage === "fallback") || draftEl.value === mem.sentDraft;
  }

  let feedbackEl = null;
  function renderFeedbackStatus() {
    if (!feedbackEl) return;
    const fb = mem.feedback;
    if (!fb) {
      setChildren(feedbackEl, null);
      return;
    }
    if (fb.accepted && fb.copied === false) setChildren(feedbackEl, tag("복사는 직접 해 주세요", "warn"));
    else if (fb.accepted) setChildren(feedbackEl, tag(fb.edited ? "✓ 고쳐서 복사함" : "✓ 복사함", "ok"));
    else setChildren(feedbackEl, tag(`나답지 않아요${fb.reasonCode ? ` · ${reasonLabel(fb.reasonCode)}` : ""}`, "warn"));
  }

  // ---------- 결과 영역 ----------
  function renderIdle() {
    return h(
      "div",
      { class: "card output-idle" },
      mem.notice ? h("p", { class: "note note-warn", role: "status" }, mem.notice) : null,
      h("h2", { class: "output-title" }, "여기에 추천 문장이 나타나요"),
      h(
        "ul",
        { class: "promise-list" },
        h("li", null, h("strong", null, "사실은 그대로"), " — 날짜·숫자·약속이 바뀌면 추천하지 않고 초안을 그대로 둬요."),
        h("li", null, h("strong", null, "내 말투로"), " — 상대마다 정해 둔 말투와 내가 실제로 쓴 표현을 따라요."),
        h("li", null, h("strong", null, "자동으로 보내지 않아요"), " — 복사까지만 해요. 전송은 직접 합니다."),
      ),
    );
  }

  function renderLoading() {
    const contact = currentContact();
    return h(
      "div",
      { class: "card output-loading" },
      h("h2", { class: "output-title" }, `${contact ? contact.alias : "상대"}에게 맞게 바꾸는 중…`),
      skeleton(3, "추천 문장을 만드는 중이에요"),
      h("p", { class: "muted" }, "초안은 입력란에 그대로 있어요."),
      h(
        "div",
        { class: "result-actions" },
        h(
          "button",
          {
            type: "button",
            class: "btn",
            onClick: () => {
              cancelInFlight();
              mem.stage = "idle";
              renderOutput();
              draftEl.focus();
            },
          },
          "취소 ",
          kbd("Esc"),
        ),
      ),
    );
  }

  function renderError() {
    const view = mem.error;
    const contact = currentContact();
    const actions = [];
    if (view.action === "persona" && contact) {
      actions.push(
        h("a", { class: "btn btn-primary", "data-act": "goto", href: buildHash("contact", { id: contact.id }) }, "말투 설정하러 가기"),
      );
    } else if (view.action === "advanced") {
      actions.push(
        h(
          "button",
          {
            type: "button",
            class: "btn btn-primary",
            "data-act": "goto",
            onClick: () => {
              mem.advancedOpen = true;
              advancedEl.open = true;
              advancedEl.querySelector("input[type='radio']:checked, input[type='radio']")?.focus();
            },
          },
          "조건 고르기",
        ),
      );
    } else if (view.action === "retry") {
      actions.push(h("button", { type: "button", class: "btn btn-primary", "data-act": "retry", onClick: () => run() }, "다시 시도 ", kbd("R")));
    }
    actions.push(h("button", { type: "button", class: "btn", "data-act": "close", onClick: closeResult }, "닫기 ", kbd("Esc")));
    return h(
      "div",
      { class: "card output-error", role: "alert" },
      h("p", null, tag("만들지 못했어요", "warn")),
      h("h2", { class: "output-title" }, view.title),
      h("p", null, view.body),
      view.kind === "no_persona"
        ? h("p", { class: "muted" }, "설정 없이 써 보려면 '고급 · 실험 조건'에서 'A 기준선'을 고르면 돼요.")
        : null,
      h("div", { class: "result-actions" }, actions),
    );
  }

  function renderFallback() {
    const rec = mem.rec;
    staleEl = h("p", { class: "note", hidden: true }, "초안을 고쳤어요. 아래 안내는 고치기 전 초안에 대한 거예요.");
    return h(
      "article",
      { class: "card result result-fallback" },
      staleEl,
      fallbackNode(rec.result),
      h(
        "div",
        { class: "result-actions" },
        h("button", { type: "button", class: "btn btn-primary", "data-act": "retry", onClick: () => run() }, "다시 만들기 ", kbd("R")),
        h("button", { type: "button", class: "btn", "data-act": "close", onClick: closeResult }, "닫기 ", kbd("Esc")),
      ),
      extrasNode(rec, mem.sentCondition),
    );
  }

  function renderResult() {
    const rec = mem.rec;
    const contact = currentContact();
    const titleId = uid("result-title");
    staleEl = h("p", { class: "note", hidden: true }, "초안을 고쳤어요. 아래 문장은 고치기 전 초안으로 만든 거예요.");
    feedbackEl = h("span", { class: "result-status" });

    let body;
    if (mem.editing) {
      const editId = uid("edit");
      editEl = h("textarea", {
        id: editId,
        class: "textarea result-edit",
        rows: 5,
        spellcheck: "false",
        value: rec.result.result_text,
      });
      body = h("div", { class: "field" }, h("label", { for: editId }, "문장 고치기"), editEl);
    } else {
      editEl = null;
      body = sentenceNode(rec.result);
    }

    const adjustMissing = mem.askedAdjust && !rec.result.adjust;
    const rejectPanelId = uid("reject");
    const rejectPanel = h(
      "div",
      { class: "reject-panel", id: rejectPanelId, hidden: !mem.rejectOpen, role: "group", "aria-label": "나답지 않은 이유" },
      h("p", { class: "reject-title" }, "어떤 점이 나답지 않았나요? 하나를 고르면 바로 기록해요."),
      h(
        "div",
        { class: "chip-row" },
        REASON_CODES.map((r) => h("button", { type: "button", class: "chip", onClick: () => reject(r.value) }, r.label)),
      ),
    );

    const card = h(
      "article",
      { class: "card result", "aria-labelledby": titleId },
      h(
        "header",
        { class: "result-head" },
        h("h2", { class: "output-title", id: titleId }, `${contact ? contact.alias : "상대"}에게 맞춘 문장`),
        feedbackEl,
      ),
      staleEl,
      adjustMissing
        ? h("p", { class: "note note-warn" }, `'${adjustLabel(mem.askedAdjust)}'는 지금 쓸 수 없어서, 같은 설정으로 다시 만들었어요.`)
        : null,
      // 자동으로 골랐는데 A(기준선)가 됐다면 말투 설정이 없다는 뜻이다 — 왜 내 말투가 아닌지 알려 준다
      mem.sentCondition === "auto" && rec.condition === "A" && contact && contact.persona_version == null
        ? h(
            "p",
            { class: "note" },
            "이 상대의 말투 설정이 없어서 일반적인 다듬기만 했어요. ",
            h("a", { href: buildHash("contact", { id: contact.id }) }, "말투 설정하기"),
          )
        : null,
      body,
      keptNode(rec.result),
      warningsNode(rec.result),
      h(
        "div",
        { class: "result-actions" },
        h(
          "button",
          { type: "button", class: "btn btn-primary btn-lg", "data-act": "copy", onClick: copy },
          mem.editing ? "고친 문장 복사 " : "복사 ",
          mem.editing ? kbd(modKey, "Enter") : kbd("Enter"),
        ),
        mem.editing
          ? h("button", { type: "button", class: "btn", onClick: () => toggleEdit(false) }, "고치기 취소 ", kbd("Esc"))
          : h("button", { type: "button", class: "btn", "data-act": "edit", onClick: () => toggleEdit(true) }, "직접 고치기"),
        h("button", { type: "button", class: "btn", "data-act": "regen", onClick: () => run() }, "다시 만들기 ", kbd("R")),
      ),
      h(
        "div",
        { class: "result-adjust", role: "group", "aria-label": "조금만 바꾸기" },
        h("span", { class: "adjust-label" }, "조금만 바꾸기"),
        ADJUSTS.map((a) =>
          h("button", { type: "button", class: "chip", "data-adjust": a.value, onClick: () => run({ adjust: a.value }) }, a.label, " ", kbd(a.key)),
        ),
      ),
      h(
        "div",
        { class: "result-foot" },
        h("button", { type: "button", class: "btn btn-quiet", "data-act": "close", onClick: closeResult }, "초안 그대로 ", kbd("Esc")),
        h(
          "button",
          {
            type: "button",
            class: "btn btn-quiet",
            "data-act": "reject",
            "aria-expanded": String(mem.rejectOpen),
            "aria-controls": rejectPanelId,
            onClick: () => {
              mem.rejectOpen = !mem.rejectOpen;
              renderOutput();
              const next = mem.rejectOpen ? outEl.querySelector(".reject-panel .chip") : outEl.querySelector("[data-act='reject']");
              next?.focus();
            },
          },
          "나답지 않아요",
        ),
        h("p", { class: "promise" }, "복사까지만 해요. 전송은 직접 합니다."),
      ),
      rejectPanel,
      extrasNode(rec, mem.sentCondition),
    );
    return card;
  }

  function renderOutput() {
    staleEl = null;
    feedbackEl = null;
    editEl = null;
    let node;
    if (mem.stage === "loading") node = renderLoading();
    else if (mem.stage === "result" && mem.rec) node = renderResult();
    else if (mem.stage === "fallback" && mem.rec) node = renderFallback();
    else if (mem.stage === "error" && mem.error) node = renderError();
    else node = renderIdle();
    setChildren(outEl, node);
    // 한 화면에 주 버튼은 하나: 결과·안내 카드에 주 행동(복사, 다시 만들기 등)이 생기면 "말투 맞추기"는 보조 버튼으로 내린다
    runBtn.classList.toggle("btn-primary", mem.stage === "idle" || mem.stage === "loading");
    renderFeedbackStatus();
    updateStaleNote();
  }

  // ---------- 키보드 ----------
  function onKeydown(e) {
    if (destroyed || dialogOpen() || !root.isConnected) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key === "Enter" && !e.altKey && !e.shiftKey) {
      // 글자를 넣는 키가 아니므로 한글 조합 중에도 받는다(입력란의 값에는 조합 중인 글자가 이미 들어 있다)
      if (editEl && e.target === editEl) {
        e.preventDefault();
        copy();
      } else if (e.target === document.body || form.contains(e.target)) {
        e.preventDefault();
        run();
      }
      return;
    }
    if (e.isComposing || e.keyCode === 229) return; // 한글 조합 중인 글자는 가로채지 않는다
    if (mod || e.altKey) return;
    // 이 화면 안(또는 아무 데도 포커스가 없을 때)에서 누른 키만 받는다 — 왼쪽 메뉴·계정 메뉴에서는 끼어들지 않는다
    if (e.target !== document.body && !(e.target instanceof Node && root.contains(e.target))) return;

    if (e.key === "Escape") {
      if (mem.stage === "idle") return;
      if (editEl && e.target === editEl) {
        e.preventDefault();
        toggleEdit(false);
        return;
      }
      if (isTypingTarget(e.target)) return; // 초안을 쓰는 중의 Esc는 건드리지 않는다
      e.preventDefault();
      if (mem.stage === "loading") {
        cancelInFlight();
        mem.stage = "idle";
        renderOutput();
        draftEl.focus();
      } else {
        closeResult();
      }
      return;
    }

    if (!["result", "fallback", "error"].includes(mem.stage)) return;
    if (isTypingTarget(e.target)) return; // 입력 중인 글자는 가로채지 않는다
    const onControl = e.target instanceof Element && e.target.closest("button, a, summary, [role='option'], input, select");

    if (e.key === "Enter") {
      if (onControl) return; // 포커스된 버튼·링크의 Enter는 그 버튼 몫(복사 버튼이면 그대로 복사)
      if (mem.stage === "result") {
        e.preventDefault();
        copy();
      }
      return;
    }
    const code = e.code || "";
    const key = (e.key || "").toLowerCase();
    if (code === "KeyR" || key === "r") {
      if (mem.stage === "error" && mem.error?.action !== "retry") return;
      e.preventDefault();
      run();
      return;
    }
    if (mem.stage !== "result" || mem.editing) return;
    const adjust = ADJUSTS.find((a) => a.key === e.key || code === `Digit${a.key}` || code === `Numpad${a.key}`);
    if (adjust) {
      e.preventDefault();
      run({ adjust: adjust.value });
    }
  }
  document.addEventListener("keydown", onKeydown);

  // ---------- 시작 ----------
  renderSituationChips();
  renderRunButton(false);
  renderAdvanced();
  updateCounter();
  setChildren(pickerBox, skeleton(2, "상대 목록을 불러오는 중이에요"));
  if (mem.stage === "loading") mem.stage = "idle"; // 이전 화면에서 끊긴 요청
  renderOutput();

  const start = async () => {
    try {
      await loadContacts();
    } catch (err) {
      if (destroyed || err.status === 401) return;
      setChildren(pickerBox, errorState(err.message, start));
      return;
    }
    if (destroyed) return;
    const list = contacts();
    if (mem.contactId && !list.some((c) => c.id === mem.contactId)) {
      Object.assign(mem, { contactId: null, stage: "idle", rec: null, error: null, feedback: null });
      renderOutput();
    }
    if (!mem.contactId) {
      const last = recentContactIds().find((cid) => list.some((c) => c.id === cid));
      if (last) mem.contactId = last;
      else if (list.length === 1) mem.contactId = list[0].id;
    }
    renderPicker();
    // 상대가 정해져 있으면 바로 쓸 수 있게 초안 입력란에 커서를 둔다
    if (mem.stage === "idle" && mem.contactId && !dialogOpen()) draftEl.focus({ preventScroll: true });
  };
  start();

  return () => {
    destroyed = true;
    seq += 1;
    if (controller) controller.abort();
    if (mem.stage === "loading") mem.stage = "idle";
    mem.editing = false;
    document.removeEventListener("keydown", onKeydown);
  };
}
