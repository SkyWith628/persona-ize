// 페르소나(상대별 말투 설정) 편집. 상대 상세 화면 안에 들어간다.
//  - 격식·친근함·직접성은 5단계 고르기(단계마다 말로 된 설명). 0~100 슬라이더는 쓰지 않는다.
//  - 저장하면 새 버전이 된다(예전 버전은 기록에 남는다). 다른 곳에서 먼저 저장했으면(409) 고르게 한다.
//  - "과거 대화 기준 제안"은 근거와 함께 보여 주고, 적용은 사용자가 누를 때만. 저장은 따로.

import { api } from "../api.js";
import { isMissingEndpoint } from "../lib/errors.js";
import { formatDateTime, formatNumber } from "../lib/format.js";
import {
  EMOJI_OPTIONS,
  LENGTH_OPTIONS,
  LIMITS,
  SCALES,
  applySignals,
  describeSettings,
  normalizeSettings,
  settingsEqual,
  signalMatches,
  signalView,
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
    h("legend", null, legend, sub ? h("span", { class: "legend-sub" }, sub) : null),
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
  async function save(settings = readForm()) {
    try {
      const saved = await api.contacts.savePersona(contact.id, settings, persona?.version ?? 0);
      accept(saved, `말투 설정을 저장했어요 (v${saved.version}).`);
      return true;
    } catch (err) {
      if (err.status === 409) await showConflict();
      else if (err.status !== 401) toast(err.message || "저장하지 못했어요.", { kind: "error" });
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
    const mine = readForm();
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
                  persona = latest; // 최신 버전을 기준으로 내 값을 다시 저장한다
                  await busy(e.currentTarget, "저장하는 중…", () => save(mine));
                },
              },
              `내 변경으로 덮어쓰기 (새 버전 v${latest.version + 1})`,
            ),
          )
        : null,
    );
    conflictEl.hidden = false;
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
        toast(isMissingEndpoint(err) ? UNAVAILABLE_TOAST : err.message, { kind: isMissingEndpoint(err) ? "warn" : "error" });
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

  function renderSuggestion(data) {
    const count = Number(data?.based_on_messages) || 0;
    const signals = (Array.isArray(data?.signals) ? data.signals : []).filter((s) => signalView(s).usable);
    if (!count || !signals.length) {
      return emptyState(
        count ? "지금 설정을 바꿀 만한 뚜렷한 특징을 찾지 못했어요" : "이 상대에게 보낸 내 메시지가 아직 없어요",
        count ? `내 메시지 ${formatNumber(count)}개를 봤어요.` : "대화를 가져오면 내 말투를 읽어 제안해 드려요.",
        count ? null : h("a", { class: "btn", href: buildHash("import", {}, { contact: contact.id }) }, "대화 가져오기"),
      );
    }
    const current = readForm();
    return [
      h(
        "p",
        null,
        `이 상대에게 보낸 내 메시지 ${formatNumber(count)}개에서 읽은 문체예요. 성격을 추정하지 않고 말투의 특징만 봐요.`,
      ),
      h(
        "ul",
        { class: "signal-list" },
        signals.map((sig) => {
          const v = signalView(sig);
          const same = signalMatches(current, sig);
          return h(
            "li",
            { class: "signal" },
            h("div", { class: "signal-main" }, h("strong", null, v.fieldLabel), " → ", h("span", { class: "signal-value" }, v.valueLabel)),
            h("p", { class: "signal-reason" }, h("span", { class: "muted" }, "근거: "), v.reason || "근거 설명이 없어요."),
            same
              ? tag("지금 설정과 같아요")
              : h("button", { type: "button", class: "btn", onClick: () => applySuggestion([sig]) }, "적용"),
          );
        }),
      ),
      h(
        "div",
        { class: "form-actions" },
        h("button", { type: "button", class: "btn", onClick: () => applySuggestion(signals) }, "모두 적용"),
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
      note("적용해도 바로 저장되지 않아요. 값을 확인한 뒤 아래 '저장'을 눌러 주세요."),
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
                        else if (err.status !== 401) toast(err.message || "되돌리지 못했어요.", { kind: "error" });
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
