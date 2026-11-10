// 잠깐 떴다 사라지는 알림. #toasts는 aria-live 영역이라 화면 낭독기가 읽어 준다.

import { h } from "./dom.js";

const ICON = { ok: "✓", error: "!", warn: "!", info: "" };
const LABEL = { ok: "완료", error: "오류", warn: "확인", info: "알림" };

function region() {
  let el = document.getElementById("toasts");
  if (!el) {
    el = h("div", { id: "toasts", class: "toasts", "aria-live": "polite", "aria-relevant": "additions" });
    document.body.append(el);
  }
  return el;
}

/**
 * @param {string} message
 * @param {{kind?: "info"|"ok"|"warn"|"error", timeout?: number}} [opts] timeout 0이면 닫기를 누를 때까지 둔다
 */
export function toast(message, opts = {}) {
  const kind = opts.kind || "info";
  const timeout = opts.timeout ?? (kind === "error" || kind === "warn" ? 9000 : 5000);
  let timer = null;
  const close = () => {
    if (timer) clearTimeout(timer);
    el.remove();
  };
  const el = h(
    "div",
    { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" },
    ICON[kind] ? h("span", { class: "toast-icon", "aria-hidden": "true" }, ICON[kind]) : null,
    h("span", { class: "sr-only" }, `${LABEL[kind]}: `),
    h("span", { class: "toast-text" }, message),
    h("button", { type: "button", class: "toast-close", "aria-label": "알림 닫기", onClick: close }, "✕"),
  );
  const host = region();
  while (host.children.length >= 4) host.firstElementChild.remove();
  host.append(el);
  if (timeout > 0) timer = setTimeout(close, timeout);
  return close;
}
