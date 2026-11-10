// 확인 대화상자. 브라우저의 <dialog>(showModal)를 쓴다: 뒤 화면이 눌리지 않고, Esc로 닫히고,
// 닫으면 열기 전에 있던 자리로 포커스가 돌아간다. Tab은 대화상자 안에서만 돈다.

import { focusables, h, uid } from "./dom.js";

let openCount = 0;

/** 지금 대화상자가 열려 있는가(화면 단축키가 끼어들지 않게). */
export function dialogOpen() {
  return openCount > 0;
}

/**
 * @param {object} opts
 * @param {string} opts.title
 * @param {(api: {close: (value?: any) => void, dialog: HTMLDialogElement}) => Node|Node[]} opts.render 본문과 버튼을 만든다
 * @param {boolean} [opts.danger]
 * @param {string} [opts.size] "wide"
 * @returns {Promise<any>} close(value)에 넘긴 값. Esc·바깥 닫기는 undefined.
 */
export function openDialog({ title, render, danger = false, size = "" }) {
  return new Promise((resolve) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const titleId = uid("dlg");
    let result;
    const dialog = h("dialog", {
      class: ["dialog", danger ? "dialog-danger" : "", size ? `dialog-${size}` : ""],
      "aria-labelledby": titleId,
    });
    const close = (value) => {
      result = value;
      dialog.close();
    };
    const body = h("div", { class: "dialog-body" }, render({ close, dialog }));
    dialog.append(h("h2", { class: "dialog-title", id: titleId }, title), body);

    dialog.addEventListener("keydown", (e) => {
      if (e.key !== "Tab") return;
      const items = focusables(dialog);
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
    // 처리 중(예: 삭제 요청을 보낸 뒤)에는 Esc로 닫히지 않게 한다 — 결과를 놓치면 화면과 서버가 어긋난다
    dialog.addEventListener("cancel", (e) => {
      if (dialog.querySelector('[aria-busy="true"]')) e.preventDefault();
    });
    dialog.addEventListener("close", () => {
      openCount -= 1;
      dialog.remove();
      if (opener && opener.isConnected) opener.focus();
      resolve(result);
    });

    document.body.append(dialog);
    openCount += 1;
    dialog.showModal();
    const preferred = dialog.querySelector("[data-autofocus]") || focusables(body)[0];
    if (preferred) preferred.focus();
  });
}

/**
 * 예/아니오 확인.
 * requireText를 주면 그 글자를 똑같이 입력해야 확인 버튼이 눌린다(되돌릴 수 없는 삭제).
 * @returns {Promise<boolean>}
 */
export function confirmDialog({
  title,
  message = null,
  bullets = [],
  confirmLabel = "확인",
  cancelLabel = "취소",
  danger = false,
  requireText = "",
  requireLabel = "",
}) {
  return openDialog({
    title,
    danger,
    render: ({ close }) => {
      const confirm = h(
        "button",
        { type: "submit", class: danger ? "btn btn-danger-solid" : "btn btn-primary", disabled: Boolean(requireText) },
        confirmLabel,
      );
      let input = null;
      let field = null;
      if (requireText) {
        const inputId = uid("confirm");
        input = h("input", {
          id: inputId,
          class: "input",
          type: "text",
          autocomplete: "off",
          spellcheck: "false",
          "data-autofocus": true,
          onInput: () => {
            confirm.disabled = input.value.trim() !== requireText;
          },
        });
        field = h(
          "div",
          { class: "field" },
          h("label", { for: inputId }, requireLabel || `확인하려면 "${requireText}"를 입력해 주세요`),
          input,
        );
      }
      const cancel = h(
        "button",
        { type: "button", class: "btn", "data-autofocus": requireText ? null : true, onClick: () => close(false) },
        cancelLabel,
      );
      return h(
        "form",
        {
          method: "dialog",
          class: "dialog-form",
          onSubmit: (e) => {
            e.preventDefault();
            if (!confirm.disabled) close(true);
          },
        },
        typeof message === "string" ? h("p", { class: "dialog-message" }, message) : message,
        bullets.length ? h("ul", { class: "dialog-bullets" }, bullets.map((b) => h("li", null, b))) : null,
        field,
        h("div", { class: "dialog-actions" }, cancel, confirm),
      );
    },
  }).then((value) => value === true);
}
