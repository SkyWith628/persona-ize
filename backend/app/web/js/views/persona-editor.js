// 페르소나(상대별 말투 설정) 편집. 상대 상세 화면 안에 들어간다.
//  - 격식·친근함·직접성은 5단계 고르기(단계마다 말로 된 설명). 0~100 슬라이더는 쓰지 않는다.
//  - 저장하면 새 버전이 된다(예전 버전은 기록에 남는다). 다른 곳에서 먼저 저장했으면(409) 고르게 한다.
//  - "과거 대화 기준 제안"은 근거와 함께 보여 주고, 적용은 사용자가 누를 때만. 저장은 따로.
//    말투가 바뀐 시점을 찾으면 제안 카드(이전 → 현재, 호칭, 그래프가 찾은 계기·범위 단서)를 신호 목록 위에 둔다.

import { api } from "../api.js";
import { describeError, isMissingEndpoint } from "../lib/errors.js";
import { formatDateTime, formatNumber } from "../lib/format.js";
import {
  EMOJI_OPTIONS,
  LENGTH_OPTIONS,
  LIMITS,
  SCALES,
  applySignals,
  cardSignals,
  cardView,
  describeSettings,
  isFewMessages,
  normalizeSettings,
  personaSaveError,
  settingsEqual,
  signalMatches,
  signalView,
  suggestionState,
  visibleSignals,
} from "../lib/persona.js";
import { buildHash } from "../lib/router.js";
import { invalidateContacts } from "../session.js";
import { chipsInput } from "../ui/chips-input.js";
import { confirmDialog } from "../ui/dialog.js";
import { busy, h, setChildren, uid } from "../ui/dom.js";
import { UNAVAILABLE_TOAST, emptyState, loadInto, note, tag } from "../ui/parts.js";
import { toast } from "../ui/toast.js";

/** 고르기 묶음(라디오). options: [{ value, label, desc }] */
function choiceField({ legend, sub = "", options, value, columns, onChange }) {
  const name = uid("choice");
  const descEl = h("p", { class: "choice-desc" });
  const inputs = [];
  const show = (v) => {
    descEl.textContent = options.find((o) => String(o.value) === String(v))?.desc || "";
  };
  const el = h(
    "fieldset",
    { class: "field choice" },
    // 제목과 설명 사이의 빈칸은 읽는 이름("격식 얼마나 격식을 차리나요?")이 붙지 않게 하려는 것
    h("legend", null, legend, sub ? [" ", h("span", { class: "legend-sub" }, sub)] : null),
    h(
      "div",
      { class: `seg seg-${columns}` },
      options.map((o, i) => {
        const inputId = uid("opt");
        const input = h("input", {
          type: "radio",
          class: "seg-input",
          name,
          id: inputId,
          value: String(o.value),
          // 묶음마다 같은 글자("보통")가 있어, 읽는 이름에는 어느 항목인지 붙인다("친근함: 보통")
          "aria-label": `${legend}: ${o.label}`,
          checked: String(o.value) === String(value),
          onChange: () => {
            show(o.value);
            onChange();
          },
        });
        inputs.push(input);
        return [
          input,
          h(
            "label",
            { for: inputId, class: "seg-label" },
            o.num ? h("span", { class: "seg-num", "aria-hidden": "true" }, String(i + 1)) : null,
            h("span", { class: "seg-text" }, o.label),
          ),
        ];
      }),
    ),
    descEl,
  );
  show(value);
  return {
    el,
    get: () => inputs.find((i) => i.checked)?.value ?? String(value),
    set: (v) => {
      for (const i of inputs) i.checked = i.value === String(v);
      show(v);
    },
  };
}

/**
 * @param {HTMLElement} box
 * @param {object} opts
 * @param {object} opts.contact  ContactOut
 * @param {object|null} opts.persona  PersonaOut 또는 null(아직 없음)
 * @param {(persona: object) => void} [opts.onSaved]
 */
export function mountPersonaEditor(box, { contact, persona: initial, onSaved = null }) {
  let persona = initial;
  let base = normalizeSettings(persona?.settings);
  let reloadVersions = null;

  // ---------- 입력 ----------
  const scales = SCALES.map((scale) => ({
    field: scale.field,
    control: choiceField({
      legend: scale.label,
      sub: scale.question,
      columns: 5,
      value: base[scale.field],
      options: scale.steps.map((s, i) => ({ value: i + 1, label: s.short, desc: s.desc, num: true })),
      onChange: () => updateDirty(),
    }),
  }));
  const length = choiceField({ legend: "길이", options: LENGTH_OPTIONS, value: base.length, columns: 3, onChange: () => updateDirty() });
  const emoji = choiceField({ legend: "이모지", options: EMOJI_OPTIONS, value: base.emoji, columns: 3, onChange: () => updateDirty() });

  const addressId = uid("address");
  const addressEl = h("input", {
    id: addressId,
    class: "input",
    type: "text",
    maxlength: LIMITS.address_term,
    placeholder: "예: 팀장님, 형, 유진아",
    value: base.address_term || "",
    onInput: () => updateDirty(),
  });
  const preferred = chipsInput({
    label: "즐겨 쓰는 표현",
    hint: "이 상대에게 자주 쓰는 표현. 쓰고 Enter를 누르면 추가돼요.",
    values: base.preferred_phrases,
    max: LIMITS.preferred_phrases,
    placeholder: "예: 부탁드립니다",
    onChange: () => updateDirty(),
  });
  const forbidden = chipsInput({
    label: "쓰지 않을 표현",
    hint: "이 표현이 들어간 문장은 추천하지 않아요. 쓰고 Enter를 누르면 추가돼요.",
    values: base.forbidden_phrases,
    max: LIMITS.forbidden_phrases,
    placeholder: "예: ㅋㅋ",
    onChange: () => updateDirty(),
  });
  const notesId = uid("notes");
  const notesCount = h("span", { class: "counter" });
  const notesEl = h("textarea", {
    id: notesId,
    class: "textarea",
    rows: 2,
    maxlength: LIMITS.notes,
    placeholder: "예: 업무 얘기는 결론부터. 보이고 싶은 모습을 한 줄로 적어도 좋아요.",
    value: base.notes || "",
    onInput: () => {
      notesCount.textContent = `${notesEl.value.length} / ${LIMITS.notes}`;
      updateDirty();
    },
  });
  notesCount.textContent = `${notesEl.value.length} / ${LIMITS.notes}`;

  const versionTag = h("span", { class: "tag" });
  const dirtyEl = h("span", { class: "dirty", role: "status" });
  const conflictEl = h("div", { class: "conflict", hidden: true });
  const saveBtn = h("button", { type: "submit", class: "btn btn-primary" }, "저장");
  const suggestionBox = h("div", { class: "suggestion", hidden: true });
  const versionsBody = h("div", { class: "versions-body" });

  function readForm() {
    const raw = {
      length: length.get(),
      emoji: emoji.get(),
      address_term: addressEl.value,
      preferred_phrases: preferred.get(),
      forbidden_phrases: forbidden.get(),
      notes: notesEl.value,
    };
    for (const s of scales) raw[s.field] = Number(s.control.get());
    return normalizeSettings(raw);
  }

  function fillForm(settings) {
    const s = normalizeSettings(settings);
    for (const sc of scales) sc.control.set(s[sc.field]);
    length.set(s.length);
    emoji.set(s.emoji);
    addressEl.value = s.address_term || "";
    preferred.set(s.preferred_phrases);
    forbidden.set(s.forbidden_phrases);
    notesEl.value = s.notes || "";
    notesCount.textContent = `${notesEl.value.length} / ${LIMITS.notes}`;
    updateDirty();
  }

  function updateDirty() {
    const dirty = !settingsEqual(readForm(), base);
    versionTag.textContent = persona ? `v${persona.version}` : "아직 없음";
    if (!conflictEl.hidden && conflictEl.querySelector("button")) {
      // 다른 곳에서 먼저 저장된 상태: 아래 저장 버튼 대신 안내 상자의 두 가지 중에서 고른다
      dirtyEl.textContent = "위 안내에서 어떻게 할지 골라 주세요.";
      saveBtn.textContent = "저장";
      saveBtn.disabled = true;
      return;
    }
    if (!persona) {
      dirtyEl.textContent = "아직 저장한 말투 설정이 없어요. 값을 확인하고 저장하면 v1이 돼요.";
      saveBtn.textContent = "저장해서 v1 만들기";
      saveBtn.disabled = false;
      return;
    }
    dirtyEl.textContent = dirty ? "저장하지 않은 변경이 있어요." : "";
    saveBtn.textContent = dirty ? `저장 (새 버전 v${persona.version + 1})` : "저장";
    saveBtn.disabled = !dirty;
  }

  function accept(saved, message) {
    persona = saved;
    base = normalizeSettings(saved.settings);
    conflictEl.hidden = true;
    fillForm(base);
    invalidateContacts();
    toast(message, { kind: "ok" });
    onSaved?.(saved);
    reloadVersions?.();
  }

  // ---------- 저장·충돌 ----------
  /** 저장 실패 알림. 서버가 어느 항목 때문인지 알려 주면(422) 그 항목 이름으로 말하고, 표현 칸이면 칸 아래에도 적는다. */
  function showSaveError(err, fallback) {
    const rejected = personaSaveError(err);
    if (rejected?.field === "preferred_phrases") preferred.setError(rejected.detail);
    if (rejected?.field === "forbidden_phrases") forbidden.setError(rejected.detail);
    toast(rejected ? rejected.message : describeError(err, fallback), { kind: "error" });
  }

  async function save(settings = readForm()) {
    try {
      const saved = await api.contacts.savePersona(contact.id, settings, persona?.version ?? 0);
      accept(saved, `말투 설정을 저장했어요 (v${saved.version}).`);
      return true;
    } catch (err) {
      if (err.status === 409) await showConflict();
      else if (err.status !== 401) showSaveError(err, "저장하지 못했어요.");
      return false;
    }
  }

  async function showConflict() {
    let latest = null;
    try {
      latest = await api.contacts.persona(contact.id);
    } catch {
      latest = null;
    }
    setChildren(
      conflictEl,
      h("p", { class: "conflict-title" }, h("span", { "aria-hidden": "true" }, "! "), "다른 곳에서 먼저 저장했어요"),
      h(
        "p",
        null,
        latest
          ? `서버에는 더 새 버전(v${latest.version})이 있어요. 내가 고친 값은 아직 화면에 그대로 있어요.`
          : "최신 설정을 읽지 못했어요. 새로 고친 뒤 다시 시도해 주세요.",
      ),
      latest
        ? h(
            "div",
            { class: "form-actions" },
            h(
              "button",
              {
                type: "button",
                class: "btn",
                onClick: () => {
                  persona = latest;
                  base = normalizeSettings(latest.settings);
                  conflictEl.hidden = true;
                  fillForm(base);
                  reloadVersions?.();
                  toast(`최신 설정(v${latest.version})을 불러왔어요.`, { kind: "info" });
                },
              },
              "최신 설정 불러오기",
            ),
            h(
              "button",
              {
                type: "button",
                class: "btn",
                onClick: async (e) => {
                  persona = latest; // 최신 버전을 기준으로, 지금 화면에 있는 내 값을 다시 저장한다
                  await busy(e.currentTarget, "저장하는 중…", () => save());
                },
              },
              `내 변경으로 덮어쓰기 (새 버전 v${latest.version + 1})`,
            ),
          )
        : null,
    );
    conflictEl.hidden = false;
    updateDirty();
    conflictEl.querySelector("button")?.focus();
  }

  // ---------- 관계 기본값·제안 ----------
  async function resetToDefault(button) {
    await busy(button, "불러오는 중…", async () => {
      try {
        const defaults = await api.personaDefaults();
        const settings = defaults?.[contact.relationship_type];
        if (!settings) {
          toast("이 관계의 기본값이 없어요.", { kind: "warn" });
          return;
        }
        fillForm(settings);
        toast("관계 기본값을 불러왔어요. 저장을 눌러야 반영돼요.", { kind: "info" });
      } catch (err) {
        if (err.status === 401) return;
        toast(isMissingEndpoint(err) ? UNAVAILABLE_TOAST : describeError(err), { kind: isMissingEndpoint(err) ? "warn" : "error" });
      }
    });
  }

  function applySuggestion(signals) {
    const { settings, applied } = applySignals(readForm(), signals);
    if (!applied.length) return;
    fillForm(settings);
    toast("제안을 적용했어요. 저장을 눌러야 반영돼요.", { kind: "info" });
    openSuggestion(); // "지금 설정과 같아요" 표시를 새로 고친다
    (suggestionBox.querySelector(".signal .btn") || saveBtn).focus();
  }

  // "그대로 두기"를 고른 카드는 이 화면을 떠날 때까지 다시 묻지 않는다(아무것도 저장하지 않는다)
  let cardDismissed = false;

  /** 제안 카드: 말투 이전 → 현재, 호칭, 그래프가 찾은 계기·범위 단서. 값을 채우는 것은 버튼을 누를 때만. */
  function renderCard(card, current) {
    const view = cardView(card, contact.alias);
    if (!view) return null;
    if (cardDismissed) return note("말투 변화 제안은 그대로 두기로 했어요. 설정은 바뀌지 않았어요.");
    const proposed = cardSignals(card);
    const same = proposed.length > 0 && proposed.every((sig) => signalMatches(current, sig));
    const titleId = uid("card-title");
    const keep = h(
      "button",
      {
        type: "button",
        class: "btn btn-quiet",
        "aria-label": "말투 변화 제안 그대로 두기",
        onClick: () => {
          cardDismissed = true;
          openSuggestion();
          (suggestionBox.querySelector(".signal .btn, .form-actions .btn") || suggestBtn).focus();
        },
      },
      "그대로 두기",
    );
    let question = null;
    if (view.ask) {
      // 상황에 따라 다른 말투일 수 있으면 값을 정해 주지 않고 묻는다 — 왜 묻는지부터 말한다
      const tail = same ? "" : proposed.length ? "어떻게 할까요?" : "값은 정해 드리지 않았어요. 아래에서 직접 골라 주세요.";
      question = [view.askReason, tail].filter(Boolean).join(" ");
    }
    return h(
      "section",
      { class: "suggestion-card", "aria-labelledby": titleId },
      h("p", { class: "suggestion-card-title", id: titleId }, view.title),
      h(
        "dl",
        { class: "suggestion-card-rows" },
        view.rows.map((row) => [h("dt", null, row.label), h("dd", null, row.text)]),
      ),
      question ? h("p", { class: "suggestion-card-ask" }, question) : null,
      h(
        "div",
        { class: "form-actions" },
        same
          ? tag("지금 설정과 같아요")
          : proposed.length
            ? h(
                "button",
                { type: "button", class: "btn", "aria-label": `${view.applyLabel} (말투 변화 제안 적용)`, onClick: () => applySuggestion(proposed) },
                view.applyLabel,
              )
            : null,
        same ? null : keep,
      ),
    );
  }

  function renderSuggestion(data) {
    const count = Number(data?.based_on_messages) || 0;
    const signals = visibleSignals(data);
    const state = suggestionState(data);
    const importLink = () =>
      h("a", { class: "btn", href: buildHash("import", {}, { contact: contact.id }), "aria-label": `${contact.alias} 대화 가져오기` }, "대화 가져오기");
    if (state === "none") {
      return emptyState("이 상대에게 보낸 내 메시지가 아직 없어요", "대화를 가져오면 내 말투를 읽어 제안해 드려요.", importLink());
    }
    if (state === "few") {
      // 표본이 적으면 우연한 말버릇을 말투로 읽기 쉽다 — 제안하지 않는다
      return emptyState(
        "과거 대화가 더 쌓이면 제안해 드려요",
        `지금은 내 메시지가 ${formatNumber(count)}개뿐이라 말투를 읽기에 모자라요.`,
        importLink(),
      );
    }
    if (state === "same") {
      return emptyState("지금 설정을 바꿀 만한 뚜렷한 특징을 찾지 못했어요", `내 메시지 ${formatNumber(count)}개를 봤어요.`);
    }
    const current = readForm();
    // 표본이 적어 전체 문체는 읽지 않았더라도, 말투가 바뀐 시점과 그래프 근거는 따로 보여 준다
    const lead = isFewMessages(data)
      ? `내 메시지가 ${formatNumber(count)}개뿐이라 전체 문체는 읽지 않았어요. 말투가 바뀐 시점과 그 근거만 보여 드려요.`
      : `이 상대에게 보낸 내 메시지 ${formatNumber(count)}개에서 읽은 문체예요. 성격을 추정하지 않고 말투의 특징만 봐요.`;
    return [
      h("p", null, lead),
      renderCard(data?.card, current),
      signals.length
        ? h(
            "ul",
            { class: "signal-list" },
            signals.map((sig) => {
              const v = signalView(sig);
              const same = signalMatches(current, sig);
              return h(
                "li",
                { class: "signal" },
                h(
                  "div",
                  { class: "signal-main" },
                  h("strong", null, v.fieldLabel),
                  " → ",
                  h("span", { class: "signal-value" }, v.valueLabel),
                  " ",
                  // 어디서 읽은 제안인지: 내 메시지 전체 · 말투 변화 · 그래프
                  h("span", { class: "tag signal-source" }, h("span", { class: "sr-only" }, "출처: "), v.sourceLabel),
                  v.since ? [" ", h("span", { class: "signal-since" }, `${v.since}부터`)] : null,
                ),
                h("p", { class: "signal-reason" }, h("span", { class: "muted" }, "근거: "), v.reason || "근거 설명이 없어요."),
                same
                  ? tag("지금 설정과 같아요")
                  : h(
                      "button",
                      // 제안마다 같은 글자라, 읽는 이름에는 무엇을 적용하는지 붙인다
                      { type: "button", class: "btn", "aria-label": `${v.fieldLabel} ${v.valueLabel} 적용`, onClick: () => applySuggestion([sig]) },
                      "적용",
                    ),
              );
            }),
          )
        : null,
      h(
        "div",
        { class: "form-actions" },
        signals.length ? h("button", { type: "button", class: "btn", onClick: () => applySuggestion(signals) }, "모두 적용") : null,
        h(
          "button",
          {
            type: "button",
            class: "btn btn-quiet",
            onClick: () => {
              suggestionBox.hidden = true;
              suggestBtn.focus();
            },
          },
          "닫기",
        ),
      ),
      // 적용할 것이 남아 있을 때만 안내한다(카드를 그대로 두기로 했고 신호도 없으면 적용할 것이 없다)
      signals.length || !cardDismissed ? note("적용해도 바로 저장되지 않아요. 값을 확인한 뒤 아래 '저장'을 눌러 주세요.") : null,
    ];
  }

  let suggestionData = null;
  function openSuggestion() {
    suggestionBox.hidden = false;
    if (suggestionData) {
      setChildren(suggestionBox, h("h3", { class: "section-sub" }, "과거 대화 기준 제안"), renderSuggestion(suggestionData));
      return;
    }
    const body = h("div");
    setChildren(suggestionBox, h("h3", { class: "section-sub" }, "과거 대화 기준 제안"), body);
    loadInto(body, {
      lines: 3,
      load: async () => {
        suggestionData = await api.contacts.personaSuggestion(contact.id);
        return suggestionData;
      },
      render: renderSuggestion,
      unavailableTitle: "과거 대화 기준 제안은 지금 쓸 수 없어요",
    });
  }

  // ---------- 버전 기록 ----------
  function renderVersions(list) {
    const versions = Array.isArray(list) ? list : [];
    if (!versions.length) return emptyState("아직 저장한 버전이 없어요");
    return h(
      "ol",
      { class: "version-list" },
      versions.map((v) => {
        const current = persona && v.version === persona.version;
        return h(
          "li",
          { class: "version" },
          h(
            "div",
            { class: "version-head" },
            h("strong", null, `v${v.version}`),
            current ? tag("지금 쓰는 버전", "accent") : null,
            h("span", { class: "muted" }, formatDateTime(v.created_at)),
          ),
          h("p", { class: "version-desc" }, describeSettings(v.settings)),
          current
            ? null
            : h(
                "button",
                {
                  type: "button",
                  class: "btn",
                  // 버전마다 같은 글자라, 읽는 이름에는 몇 번째 버전인지 붙인다
                  "aria-label": `이 버전으로 되돌리기 (v${v.version})`,
                  onClick: async (e) => {
                    const button = e.currentTarget;
                    const dirty = persona && !settingsEqual(readForm(), base);
                    if (dirty) {
                      const ok = await confirmDialog({
                        title: `v${v.version} 설정으로 되돌릴까요?`,
                        message: "저장하지 않은 변경은 사라져요. 되돌린 설정은 새 버전으로 저장돼요.",
                        confirmLabel: "되돌리기",
                      });
                      if (!ok) return;
                    }
                    await busy(button, "되돌리는 중…", async () => {
                      try {
                        const saved = await api.contacts.savePersona(contact.id, normalizeSettings(v.settings), persona?.version ?? 0);
                        accept(saved, `v${v.version} 설정으로 되돌렸어요 (새 버전 v${saved.version}).`);
                      } catch (err) {
                        if (err.status === 409) await showConflict();
                        else if (err.status !== 401) showSaveError(err, "되돌리지 못했어요.");
                      }
                    });
                  },
                },
                "이 버전으로 되돌리기",
              ),
        );
      }),
    );
  }

  let versionsLoaded = false;
  const versionsEl = h(
    "details",
    {
      class: "fold",
      onToggle: () => {
        if (!versionsEl.open || versionsLoaded) return;
        versionsLoaded = true;
        reloadVersions = loadInto(versionsBody, {
          lines: 3,
          load: () => api.contacts.personaVersions(contact.id),
          render: renderVersions,
          unavailableTitle: "버전 기록은 지금 볼 수 없어요",
        });
      },
    },
    h("summary", null, "버전 기록"),
    h("p", { class: "hint" }, "저장할 때마다 새 버전이 생겨요. 예전 버전으로 되돌리면 그 설정이 새 버전으로 다시 저장돼요."),
    versionsBody,
  );

  const suggestBtn = h("button", { type: "button", class: "btn", onClick: () => openSuggestion() }, "과거 대화 기준 제안 보기");

  setChildren(
    box,
    h(
      "div",
      { class: "section-head" },
      h("h2", { class: "section-title" }, "말투 설정 ", versionTag),
      h(
        "div",
        { class: "section-actions" },
        suggestBtn,
        h("button", { type: "button", class: "btn", onClick: (e) => resetToDefault(e.currentTarget) }, "관계 기본값으로 되돌리기"),
      ),
    ),
    h("p", { class: "section-lead" }, "이 상대에게 보낼 때 쓰는 말투예요. 과거에 쓴 말투와 다르면 지금 이 설정을 먼저 따라요."),
    suggestionBox,
    h(
      "form",
      {
        class: "persona-form",
        novalidate: true,
        onSubmit: async (e) => {
          e.preventDefault();
          await busy(saveBtn, "저장하는 중…", () => save());
          updateDirty();
        },
      },
      scales.map((s) => s.control.el),
      h("div", { class: "form-row" }, length.el, emoji.el),
      h(
        "div",
        { class: "field" },
        h("label", { for: addressId }, "호칭"),
        addressEl,
        h("p", { class: "hint" }, "여기 적은 호칭만 써요. 비워 두면 초안에 있는 호칭만 그대로 두고 새 호칭을 만들지 않아요."),
      ),
      preferred.el,
      forbidden.el,
      h(
        "div",
        { class: "field" },
        h("div", { class: "field-label-row" }, h("label", { for: notesId }, "메모"), notesCount),
        notesEl,
      ),
      conflictEl,
      h("div", { class: "form-actions form-actions-sticky" }, saveBtn, dirtyEl),
    ),
    versionsEl,
  );
  updateDirty();
}
