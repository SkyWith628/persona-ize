// 상대 상세(#/contacts/:id): 말투 설정(페르소나) → 과거 대화 → 이름·관계 → 삭제.

import { api } from "../api.js";
import { describeError, isUnavailable } from "../lib/errors.js";
import { formatNumber, initial, josa } from "../lib/format.js";
import { REL_LABEL, REL_ORDER, relLabel } from "../lib/model.js";
import { buildHash } from "../lib/router.js";
import { invalidateContacts } from "../session.js";
import { confirmDialog } from "../ui/dialog.js";
import { busy, h, setChildren, uid } from "../ui/dom.js";
import { emptyState, loadInto, tag } from "../ui/parts.js";
import { toast } from "../ui/toast.js";
import { mountConversations } from "./conversations.js";
import { mountPersonaEditor } from "./persona-editor.js";

function infoSection(contact, onChanged) {
  const aliasId = uid("alias");
  const relId = uid("rel");
  const aliasEl = h("input", { id: aliasId, class: "input", type: "text", maxlength: 50, required: true, value: contact.alias });
  const relEl = h(
    "select",
    { id: relId, class: "input", value: contact.relationship_type },
    REL_ORDER.map((value) => h("option", { value }, REL_LABEL[value])),
  );
  const errorEl = h("p", { class: "form-error", role: "alert", hidden: true });
  const saveBtn = h("button", { type: "submit", class: "btn" }, "이름·관계 저장");

  const onSubmit = async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    const alias = aliasEl.value.trim();
    if (!alias) {
      errorEl.textContent = "이름을 입력해 주세요.";
      errorEl.hidden = false;
      aliasEl.focus();
      return;
    }
    const patch = {};
    if (alias !== contact.alias) patch.alias = alias;
    if (relEl.value !== contact.relationship_type) patch.relationship_type = relEl.value;
    if (!Object.keys(patch).length) {
      toast("바뀐 내용이 없어요.", { kind: "info" });
      return;
    }
    await busy(saveBtn, "저장하는 중…", async () => {
      try {
        const updated = await api.contacts.update(contact.id, patch);
        invalidateContacts();
        toast("이름·관계를 저장했어요.", { kind: "ok" });
        onChanged(updated);
      } catch (err) {
        if (err.status === 401) return;
        errorEl.textContent = isUnavailable(err) ? "이름·관계 고치기는 지금 쓸 수 없어요." : describeError(err, "저장하지 못했어요.");
        errorEl.hidden = false;
      }
    });
  };

  return h(
    "section",
    { class: "card section" },
    h("h2", { class: "section-title" }, "이름·관계"),
    h(
      "form",
      { class: "inline-form", novalidate: true, onSubmit },
      h("div", { class: "field" }, h("label", { for: aliasId }, "이름"), aliasEl),
      h("div", { class: "field" }, h("label", { for: relId }, "관계"), relEl),
      saveBtn,
    ),
    h("p", { class: "hint" }, "관계를 바꿔도 저장해 둔 말투 설정은 그대로예요. 필요하면 위에서 '관계 기본값으로 되돌리기'를 눌러 주세요."),
    errorEl,
  );
}

function dangerSection(contact, navigate) {
  const remove = async () => {
    const name = contact.alias.trim();
    const ok = await confirmDialog({
      title: `'${name}'${josa(name, "을/를")} 지울까요?`,
      message: "이 상대와 관련된 것이 모두 지워져요. 되돌릴 수 없어요.",
      bullets: [
        "이 상대와의 대화방과 메시지 원문 전부",
        "대화로 만든 말투 검색 자료와 지난 약속·사건 기록",
        "말투 설정의 모든 버전",
        "이 상대에게 받은 첨삭 이력",
      ],
      confirmLabel: "상대 지우기",
      danger: true,
      requireText: name,
      requireLabel: `확인하려면 상대 이름 "${name}"${josa(name, "을/를")} 그대로 입력해 주세요`,
    });
    if (!ok) return;
    try {
      const d = await api.contacts.remove(contact.id);
      invalidateContacts();
      toast(
        `'${name}'${josa(name, "을/를")} 지웠어요 — 대화방 ${formatNumber(d.conversations)}개, 메시지 ${formatNumber(d.messages)}개, 첨삭 이력 ${formatNumber(d.rewrites)}건.`,
        { kind: "ok" },
      );
      if (Array.isArray(d.graph_errors) && d.graph_errors.length) {
        toast("지난 약속·사건 기록 일부는 지우지 못했어요. 원문과 검색 자료는 지워졌어요.", { kind: "warn", timeout: 0 });
      }
      navigate(buildHash("contacts"));
    } catch (err) {
      if (err.status !== 401) toast(describeError(err, "지우지 못했어요."), { kind: "error" });
    }
  };
  return h(
    "section",
    { class: "card section section-danger" },
    h("h2", { class: "section-title" }, "상대 삭제"),
    h("p", { class: "section-lead" }, "이 상대의 대화·말투 설정·첨삭 이력을 모두 지워요. 되돌릴 수 없어요."),
    h("button", { type: "button", class: "btn btn-danger", onClick: remove }, "이 상대 지우기"),
  );
}

export function mountContactDetail(root, ctx) {
  const contactId = ctx.route.params.id;
  const box = h("div", { class: "detail" });
  root.replaceChildren(h("p", { class: "back" }, h("a", { href: buildHash("contacts") }, "← 상대 목록")), box);

  const missing = () =>
    emptyState("이 상대를 찾을 수 없어요", "지웠거나 주소가 잘못됐어요.", h("a", { class: "btn", href: buildHash("contacts") }, "상대 목록으로"));

  function render(data) {
    if (data.missing) return missing();
    const contact = data.contact; // 이름·관계를 고치면 이 객체를 그 자리에서 바꾼다(아래 조각들이 같은 객체를 본다)
    const head = h("header", { class: "page-head" });
    const personaTag = h("span");
    const personaBox = h("section", { class: "card section" });
    const convBox = h("section", { class: "card section" });
    const infoBox = h("div");

    const setPersonaTag = (version) => {
      setChildren(personaTag, version == null ? tag("말투 설정 없음", "warn") : tag(`말투 설정 v${version}`));
    };
    const paintHead = () => {
      document.title = `${contact.alias} — 상대 — Persona_ize`;
      setChildren(
        head,
        h(
          "div",
          { class: "page-head-text detail-head" },
          h("span", { class: "avatar avatar-lg", "aria-hidden": "true" }, initial(contact.alias)),
          h(
            "div",
            null,
            h("h1", { class: "page-title", tabIndex: -1 }, contact.alias),
            h("p", { class: "page-lead" }, tag(relLabel(contact.relationship_type)), " ", personaTag),
          ),
        ),
        h(
          "div",
          { class: "page-actions" },
          h("a", { class: "btn", href: buildHash("compose", {}, { contact: contact.id }) }, "이 상대로 첨삭하기"),
        ),
      );
    };
    const paintInfo = () => {
      setChildren(
        infoBox,
        infoSection(contact, (updated) => {
          Object.assign(contact, { alias: updated.alias, relationship_type: updated.relationship_type });
          paintHead();
          paintInfo();
          head.querySelector("h1")?.focus();
        }),
      );
    };

    setPersonaTag(data.persona?.version ?? null);
    paintHead();
    paintInfo();
    mountPersonaEditor(personaBox, {
      contact,
      persona: data.persona,
      onSaved: (saved) => setPersonaTag(saved.version),
    });
    mountConversations(convBox, { contact });
    return [head, personaBox, convBox, infoBox, dangerSection(contact, ctx.navigate)];
  }

  loadInto(box, {
    lines: 6,
    load: async () => {
      let contact;
      try {
        contact = await api.contacts.get(contactId);
      } catch (err) {
        if (err.status === 404 || err.status === 422) return { missing: true }; // 없는 상대, 또는 주소의 id 모양이 틀림
        throw err;
      }
      let persona = null;
      if (contact.persona_version != null) {
        try {
          persona = await api.contacts.persona(contactId);
        } catch (err) {
          if (err.status !== 404) throw err; // 404 = 저장한 말투 설정이 없다
        }
      }
      return { contact, persona };
    },
    render,
  });

  return () => {};
}
