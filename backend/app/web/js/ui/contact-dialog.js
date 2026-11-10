// 상대 추가 대화상자(첨삭·상대·가져오기 화면이 함께 쓴다).
// 이름과 관계만 받고, 서버가 관계 유형 기본값으로 말투 설정 v1을 함께 만든다(use_default_persona).

import { api } from "../api.js";
import { REL_LABEL, REL_ORDER } from "../lib/model.js";
import { invalidateContacts, loadContacts } from "../session.js";
import { openDialog } from "./dialog.js";
import { busy, h, uid } from "./dom.js";

/** @returns {Promise<object|undefined>} 만든 상대(ContactOut). 취소하면 undefined. */
export function addContactDialog({ alias = "" } = {}) {
  return openDialog({
    title: "상대 추가",
    render: ({ close }) => {
      const aliasId = uid("alias");
      const relId = uid("rel");
      const errorEl = h("p", { class: "form-error", role: "alert", hidden: true });
      const aliasEl = h("input", {
        id: aliasId,
        class: "input",
        type: "text",
        maxlength: 50,
        required: true,
        autocomplete: "off",
        value: alias.slice(0, 50),
        placeholder: "예: 박지현 팀장님",
        "data-autofocus": true,
      });
      const relEl = h(
        "select",
        { id: relId, class: "input" },
        REL_ORDER.map((value) => h("option", { value }, REL_LABEL[value])),
      );
      const submit = h("button", { type: "submit", class: "btn btn-primary" }, "추가");

      const onSubmit = async (e) => {
        e.preventDefault();
        const name = aliasEl.value.trim();
        errorEl.hidden = true;
        if (!name) {
          errorEl.textContent = "이름을 입력해 주세요.";
          errorEl.hidden = false;
          aliasEl.focus();
          return;
        }
        await busy(submit, "추가하는 중…", async () => {
          try {
            const contact = await api.contacts.create(name, relEl.value);
            invalidateContacts();
            await loadContacts().catch(() => null);
            close(contact);
          } catch (err) {
            errorEl.textContent = err.message || "추가하지 못했어요. 잠시 뒤 다시 시도해 주세요.";
            errorEl.hidden = false;
          }
        });
      };

      return h(
        "form",
        { class: "dialog-form", onSubmit, novalidate: true },
        h(
          "div",
          { class: "field" },
          h("label", { for: aliasId }, "이름"),
          aliasEl,
          h("p", { class: "hint" }, "실명 대신 별칭을 써도 돼요. 나만 알아보면 돼요."),
        ),
        h(
          "div",
          { class: "field" },
          h("label", { for: relId }, "관계"),
          relEl,
          h("p", { class: "hint" }, "관계에 맞는 기본 말투로 시작하고, 나중에 바꿀 수 있어요."),
        ),
        errorEl,
        h(
          "div",
          { class: "dialog-actions" },
          h("button", { type: "button", class: "btn", onClick: () => close(undefined) }, "취소"),
          submit,
        ),
      );
    },
  });
}
