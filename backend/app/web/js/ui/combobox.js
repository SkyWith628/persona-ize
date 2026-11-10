// 상대 찾기: 글자를 치면 걸러지는 목록(combobox). 키보드: ↑↓ 이동, Enter 선택, Esc 닫기.

import { initial, josa } from "../lib/format.js";
import { filterContacts, relLabel } from "../lib/model.js";
import { h, setChildren, uid } from "./dom.js";

const NEW_ID = "__new__";

/**
 * @param {object} opts
 * @param {() => Array} opts.getContacts   지금의 상대 목록
 * @param {(contact) => void} opts.onSelect
 * @param {(typed: string) => void} [opts.onCreate]  "새 상대 추가"를 골랐을 때
 * @param {string} [opts.label]
 * @param {string} [opts.placeholder]
 * @returns {{el: HTMLElement, input: HTMLInputElement, isOpen: () => boolean, close: () => void}}
 */
export function contactPicker({ getContacts, onSelect, onCreate = null, label = "상대 찾기", placeholder = "이름으로 찾기" }) {
  const listId = uid("combo-list");
  let options = []; // 목록의 option 순서와 같다: [{ id, contact? }]
  let active = -1;
  let open = false;

  const input = h("input", {
    class: "input combo-input",
    type: "text",
    role: "combobox",
    autocomplete: "off",
    spellcheck: "false",
    placeholder,
    "aria-label": label,
    "aria-expanded": "false",
    "aria-controls": listId,
    "aria-autocomplete": "list",
  });
  const list = h("ul", { class: "combo-list", id: listId, role: "listbox", "aria-label": label, hidden: true });
  const el = h("div", { class: "combo" }, input, list);

  const optionNodes = () => [...list.querySelectorAll('[role="option"]')];

  function setActive(index) {
    const nodes = optionNodes();
    active = nodes.length ? Math.max(-1, Math.min(index, nodes.length - 1)) : -1;
    nodes.forEach((li, i) => {
      li.setAttribute("aria-selected", String(i === active));
      li.classList.toggle("is-active", i === active);
    });
    if (active >= 0) {
      input.setAttribute("aria-activedescendant", nodes[active].id);
      nodes[active].scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function render() {
    const typed = input.value.trim();
    const found = filterContacts(getContacts(), typed);
    options = found.map((contact) => ({ id: contact.id, contact }));
    if (onCreate) options.push({ id: NEW_ID });
    const rows = options.map((opt, i) => {
      const attrs = { role: "option", id: `${listId}-opt-${i}`, "aria-selected": "false" };
      if (opt.id === NEW_ID) {
        return h(
          "li",
          { ...attrs, class: "combo-option combo-new" },
          h("span", { class: "avatar avatar-add", "aria-hidden": "true" }, "+"),
          h("span", { class: "combo-name" }, typed ? `"${typed}" 새 상대로 추가` : "새 상대 추가"),
        );
      }
      const c = opt.contact;
      return h(
        "li",
        { ...attrs, class: "combo-option" },
        h("span", { class: "avatar", "aria-hidden": "true" }, initial(c.alias)),
        h("span", { class: "combo-name" }, c.alias),
        h("span", { class: "combo-sub" }, relLabel(c.relationship_type)),
        c.persona_version == null ? h("span", { class: "tag tag-warn" }, "말투 설정 없음") : null,
      );
    });
    if (!found.length && typed) {
      rows.unshift(h("li", { class: "combo-empty", role: "presentation" }, `"${typed}"${josa(typed, "과/와")} 맞는 상대가 없어요`));
    }
    setChildren(list, rows);
    setActive(options.length ? 0 : -1);
  }

  function show() {
    render();
    if (open) return;
    open = true;
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function close() {
    if (!open) return;
    open = false;
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function choose(index) {
    const opt = options[index];
    if (!opt) return;
    const typed = input.value.trim();
    input.value = "";
    close();
    if (opt.id === NEW_ID) onCreate?.(typed);
    else onSelect(opt.contact);
  }

  input.addEventListener("focus", show);
  input.addEventListener("click", show);
  input.addEventListener("input", show);
  input.addEventListener("blur", () => {
    setTimeout(() => {
      if (!el.contains(document.activeElement)) close();
    }, 0);
  });
  input.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return; // 한글 조합 중의 Enter·방향키는 조합에 맡긴다
    const count = options.length;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) show();
      else if (count) setActive((active + 1) % count);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) show();
      else if (count) setActive((active - 1 + count) % count);
    } else if (e.key === "Enter") {
      if (!open || active < 0) return;
      e.preventDefault();
      choose(active);
    } else if (e.key === "Escape") {
      if (!open) return;
      e.preventDefault();
      e.stopPropagation(); // 목록만 닫는다 — 화면의 Esc(결과 닫기)까지 가지 않게
      close();
    } else if (e.key === "Home" && open && count) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && open && count) {
      e.preventDefault();
      setActive(count - 1);
    }
  });
  list.addEventListener("pointerdown", (e) => e.preventDefault()); // 누르는 동안 입력칸 포커스를 잃지 않게
  list.addEventListener("click", (e) => {
    const li = e.target.closest('[role="option"]');
    if (li) choose(optionNodes().indexOf(li));
  });

  return { el, input, isOpen: () => open, close };
}
