// 관리자 화면의 확인 대화상자. 서비스 화면(js/ui/dialog.js)과 같은 방식이다: 브라우저의 <dialog>(showModal)를 써서
// 뒤 화면이 눌리지 않고, Esc로 닫히고, 닫으면 열기 전에 있던 자리로 포커스가 돌아간다.
// 되돌릴 수 없는 일(회원 삭제)은 정해 준 글자(아이디)를 그대로 입력해야 확인 버튼이 눌린다.

import { el } from "./view.js";

let seq = 0;

/**
 * @param {object} opts
 * @param {string} opts.title
 * @param {string} [opts.message]
 * @param {string[]} [opts.bullets]      지워지는 것 등 목록
 * @param {string} [opts.confirmLabel]
 * @param {string} [opts.cancelLabel]
 * @param {boolean} [opts.danger]        위험한 일(정지·삭제)이면 빨간 확인 버튼
 * @param {string} [opts.requireText]    이 글자를 똑같이 입력해야 확인할 수 있다
 * @param {string} [opts.requireLabel]   입력 칸의 라벨
 * @returns {Promise<boolean>} 확인을 눌렀으면 true. 취소·Esc는 false.
 */
export function confirmDialog({
  title,
  message = "",
  bullets = [],
  confirmLabel = "확인",
  cancelLabel = "취소",
  danger = false,
  requireText = "",
  requireLabel = "",
}) {
  return new Promise((resolve) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    seq += 1;
    const titleId = `dlg-title-${seq}`;
    const inputId = `dlg-input-${seq}`;
    let result = false;
    let done = false;

    const dialog = el("dialog", { class: danger ? "dialog danger" : "dialog", "aria-labelledby": titleId });
    const confirm = el("button", {
      type: "submit",
      class: danger ? "btn danger-solid" : "btn",
      disabled: Boolean(requireText),
      text: confirmLabel,
    });
    const cancel = el("button", { type: "button", class: "btn ghost", text: cancelLabel });

    let input = null;
    let field = null;
    if (requireText) {
      input = el("input", {
        id: inputId,
        class: "input",
        type: "text",
        autocomplete: "off",
        autocapitalize: "none",
        spellcheck: "false",
      });
      // 앞뒤 공백만 봐주고 나머지는 글자 그대로 맞아야 한다
      input.addEventListener("input", () => {
        confirm.disabled = input.value.trim() !== requireText;
      });
      field = el("div", { class: "field" }, [
        el("label", { for: inputId, text: requireLabel || `확인하려면 ${requireText} 을(를) 입력하세요` }),
        input,
      ]);
    }

    const finish = () => {
      if (done) return;
      done = true;
      dialog.remove();
      if (opener && opener.isConnected) opener.focus();
      resolve(result);
    };
    // 버튼으로 닫을 때는 곧바로 마무리한다 (브라우저의 close 이벤트는 가려진 탭에서 늦게 올 수 있다)
    const close = (value) => {
      result = value;
      if (dialog.open) dialog.close();
      finish();
    };

    const form = el("form", { method: "dialog", class: "dialog-form" }, [
      message ? el("p", { class: "dialog-message", text: message }) : null,
      bullets.length
        ? el(
            "ul",
            { class: "bullets dialog-bullets" },
            bullets.map((b) => el("li", { text: b })),
          )
        : null,
      field,
      el("div", { class: "dialog-actions" }, [cancel, confirm]),
    ]);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!confirm.disabled) close(true);
    });
    cancel.addEventListener("click", () => close(false));
    // Esc: 브라우저가 닫기 전에 오는 cancel에서 바로 마무리한다 (close 이벤트는 가려진 탭에서 한참 늦게 온다)
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      close(false);
    });
    dialog.addEventListener("close", finish); // 그 밖에 브라우저가 닫은 경우

    dialog.append(el("h2", { class: "dialog-title", id: titleId, text: title }), form);
    document.body.append(dialog);
    dialog.showModal();
    // 글자를 입력해야 하면 입력 칸으로, 아니면 '취소'로 — Enter를 잘못 눌러 바로 실행되지 않게 한다
    (input ?? cancel).focus();
  });
}
