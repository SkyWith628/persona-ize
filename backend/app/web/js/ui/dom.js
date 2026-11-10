// DOM을 만드는 작은 도우미. 문자열은 언제나 글자(text node)로 들어간다 — innerHTML을 쓰지 않는다.
// 서버가 준 이름·메시지·모델 출력이 태그로 해석되는 일이 없게 하기 위해서다(계약 §5).

const PROPS = new Set([
  "value",
  "checked",
  "disabled",
  "selected",
  "hidden",
  "readOnly",
  "required",
  "open",
  "tabIndex",
  "htmlFor",
  "className",
  "textContent",
  "autofocus",
  "multiple",
]);

function append(parent, child) {
  if (child === null || child === undefined || child === false || child === true) return;
  if (Array.isArray(child)) {
    for (const c of child) append(parent, c);
    return;
  }
  parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
}

/**
 * h("button", { class: "btn", onClick: fn, "aria-label": "닫기" }, "글자", 다른노드)
 *  - on으로 시작하고 값이 함수면 이벤트 리스너
 *  - class, dataset, style(객체) 지원
 *  - 값이 null·undefined·false인 속성은 붙이지 않는다. true면 빈 속성.
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  let selectValue; // <select>의 value는 <option>이 붙은 뒤에 넣어야 먹는다
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined) continue;
    if (key === "value" && tag === "select") {
      selectValue = value;
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "class") {
      el.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : String(value);
    } else if (key === "dataset") {
      for (const [k, v] of Object.entries(value)) if (v !== null && v !== undefined) el.dataset[k] = String(v);
    } else if (key === "style" && typeof value === "object") {
      for (const [k, v] of Object.entries(value)) el.style.setProperty(k, String(v));
    } else if (PROPS.has(key)) {
      el[key] = value;
    } else if (value === false) {
      /* 붙이지 않는다 */
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, children);
  if (selectValue !== undefined) el.value = selectValue;
  return el;
}

/** 자식을 모두 바꾼다. */
export function setChildren(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function text(value) {
  return document.createTextNode(String(value ?? ""));
}

let seq = 0;
/** 라벨과 입력을 잇는 데 쓰는 겹치지 않는 id. */
export function uid(prefix = "f") {
  seq += 1;
  return `${prefix}-${seq}`;
}

/** 누를 수 있고 보이는 요소들(포커스 가두기용). */
export function focusables(root) {
  const sel =
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
  return [...root.querySelectorAll(sel)].filter((el) => !el.hidden && el.getClientRects().length > 0);
}

/** 글자를 입력하는 자리인가(단축키가 가로채면 안 되는 곳). */
export function isTypingTarget(target) {
  if (!(target instanceof Element)) return false;
  if (target.closest("textarea, select, [contenteditable='true'], [contenteditable='']")) return true;
  const input = target.closest("input");
  if (!input) return false;
  const type = (input.getAttribute("type") || "text").toLowerCase();
  return !["checkbox", "radio", "button", "submit", "reset", "range", "file", "color"].includes(type);
}

/**
 * 버튼을 잠시 "하는 중" 상태로 두고 일을 한다. 끝나면 원래대로.
 * disabled 대신 aria-disabled를 쓴다 — disabled로 바꾸면 그 버튼에 있던 키보드 포커스가 사라진다.
 * 하는 중에 다시 눌러도 아무 일도 일어나지 않는다.
 */
export async function busy(button, label, fn) {
  if (!button) return fn();
  if (button.disabled || button.getAttribute("aria-disabled") === "true") return undefined;
  const original = [...button.childNodes];
  button.setAttribute("aria-disabled", "true");
  button.setAttribute("aria-busy", "true");
  if (label) button.replaceChildren(document.createTextNode(label));
  try {
    return await fn();
  } finally {
    button.removeAttribute("aria-disabled");
    button.removeAttribute("aria-busy");
    if (label) button.replaceChildren(...original);
  }
}
