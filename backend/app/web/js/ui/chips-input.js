// 표현 목록 입력(선호·금지 표현). 글자를 쓰고 Enter나 쉼표를 누르면 칩이 된다.

import { addPhrases } from "../lib/persona.js";
import { h, setChildren, uid } from "./dom.js";

/**
 * @param {object} opts
 * @param {string} opts.label
 * @param {string} [opts.hint]
 * @param {string[]} opts.values
 * @param {number} opts.max
 * @param {string} [opts.placeholder]
 * @param {(values: string[]) => void} [opts.onChange]
 * @returns {{el: HTMLElement, get: () => string[], set: (values: string[]) => void}}
 */
export function chipsInput({ label, hint = "", values, max, placeholder = "", onChange = null }) {
  let list = [...(values || [])];
  let pendingAfterComposition = false;
  const inputId = uid("chips");
  const hintId = uid("chips-hint");
  const chipsEl = h("ul", { class: "chips-list", "aria-label": `${label} 목록` });
  const errorEl = h("p", { class: "field-error", role: "alert", hidden: true });
  const countEl = h("span", { class: "counter" });
  const input = h("input", {
    id: inputId,
    class: "chips-entry",
    type: "text",
    autocomplete: "off",
    placeholder,
    "aria-describedby": hintId,
  });

  function showError(message) {
    errorEl.textContent = message || "";
    errorEl.hidden = !message;
  }

  function render() {
    setChildren(
      chipsEl,
      list.map((value, index) =>
        h(
          "li",
          { class: "chip-static" },
          h("span", null, value),
          h(
            "button",
            {
              type: "button",
              class: "chip-remove",
              "aria-label": `${value} 지우기`,
              onClick: () => {
                list = list.filter((_, i) => i !== index);
                showError("");
                render();
                onChange?.([...list]);
                input.focus();
              },
            },
            "✕",
          ),
        ),
      ),
    );
    countEl.textContent = `${list.length} / ${max}`;
  }

  function commit() {
    const raw = input.value;
    if (!raw.trim()) {
      input.value = "";
      return;
    }
    const before = list.length;
    const result = addPhrases(list, raw, max);
    list = result.list;
    showError(result.error);
    if (!result.error) input.value = "";
    else if (list.length > before) input.value = "";
    render();
    if (list.length !== before) onChange?.([...list]);
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault(); // 폼이 제출되지 않게
      if (e.isComposing || e.keyCode === 229) {
        pendingAfterComposition = true; // 한글 조합이 끝난 뒤에 넣는다(마지막 글자가 빠지지 않게)
        return;
      }
      commit();
    } else if (e.key === "Backspace" && !input.value && list.length && !e.isComposing) {
      list = list.slice(0, -1);
      showError("");
      render();
      onChange?.([...list]);
    }
  });
  input.addEventListener("compositionend", () => {
    if (!pendingAfterComposition) return;
    pendingAfterComposition = false;
    setTimeout(commit, 0);
  });
  input.addEventListener("input", () => {
    if (input.value.includes(",")) commit();
  });
  input.addEventListener("blur", commit);

  render();
  const el = h(
    "div",
    { class: "field" },
    h("div", { class: "field-label-row" }, h("label", { for: inputId }, label), countEl),
    h("div", { class: "chips-box", onClick: (e) => e.target === e.currentTarget && input.focus() }, chipsEl, input),
    h("p", { class: "hint", id: hintId }, hint || "쓰고 Enter를 누르면 추가돼요."),
    errorEl,
  );

  return {
    el,
    get: () => [...list],
    set: (next) => {
      list = [...(next || [])];
      showError("");
      render();
    },
  };
}
