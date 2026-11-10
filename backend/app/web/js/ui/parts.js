// 여러 화면이 함께 쓰는 조각: 제목 줄, 태그, 불러오는 중·빈 상태·오류 상태, 진행 막대.

import { describeError, isAbort, isUnavailable } from "../lib/errors.js";
import { percent } from "../lib/format.js";
import { h, setChildren } from "./dom.js";

export function pageHead(title, lead = null, actions = null) {
  return h(
    "header",
    { class: "page-head" },
    h(
      "div",
      { class: "page-head-text" },
      h("h1", { class: "page-title", tabIndex: -1 }, title),
      lead ? h("p", { class: "page-lead" }, lead) : null,
    ),
    actions ? h("div", { class: "page-actions" }, actions) : null,
  );
}

/** 상태 태그. tone: ok(지킨 것) · warn(확인할 것) · bad(오류·삭제) · accent · 없음. 색만으로 구분하지 않게 글자가 뜻을 담는다. */
export function tag(label, tone = "") {
  return h("span", { class: ["tag", tone ? `tag-${tone}` : ""] }, label);
}

export function kbd(...keys) {
  return h(
    "span",
    { class: "keys" },
    keys.map((k) => h("kbd", null, k)),
  );
}

export function skeleton(lines = 3, label = "불러오는 중이에요") {
  return h(
    "div",
    { class: "skeleton", role: "status" },
    h("span", { class: "sr-only" }, label),
    Array.from({ length: lines }, (_, i) =>
      h("span", { class: "skeleton-line", "aria-hidden": "true", style: { "--w": `${[92, 78, 60, 84, 70][i % 5]}%` } }),
    ),
  );
}

export function emptyState(title, body = null, action = null) {
  return h(
    "div",
    { class: "state state-empty" },
    h("p", { class: "state-title" }, title),
    body ? h("p", { class: "state-body" }, body) : null,
    action ? h("div", { class: "state-actions" }, action) : null,
  );
}

export function errorState(message, onRetry = null) {
  return h(
    "div",
    { class: "state state-error", role: "alert" },
    h("p", { class: "state-title" }, "불러오지 못했어요"),
    h("p", { class: "state-body" }, message),
    onRetry ? h("div", { class: "state-actions" }, h("button", { type: "button", class: "btn", onClick: onRetry }, "다시 시도")) : null,
  );
}

/** 서버에 아직 없는 기능 자리. title은 "버전 기록은 지금 볼 수 없어요"처럼 완성된 문장으로 준다. */
export function unavailable(title = "이 기능은 지금 쓸 수 없어요", body = "서버에서 준비되면 이 자리에 나타나요.") {
  return h(
    "div",
    { class: "state state-unavailable" },
    h("p", { class: "state-title" }, title),
    body ? h("p", { class: "state-body" }, body) : null,
  );
}

export const UNAVAILABLE_TOAST = "이 기능은 지금 쓸 수 없어요. 서버에서 준비되면 쓸 수 있어요.";

export function note(children, tone = "") {
  return h("p", { class: ["note", tone ? `note-${tone}` : ""] }, children);
}

/** 진행 막대. 색과 함께 숫자(label)를 같이 보여 준다. */
export function meter(done, total, label = "") {
  const pct = percent(done, total);
  return h(
    "div",
    { class: "meter", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pct, "aria-label": label || null },
    h("span", { class: "meter-bar", style: { width: `${pct}%` } }),
  );
}

/**
 * 영역 하나를 "불러오는 중 → (오류 | 없는 기능 | 빈 상태 | 내용)"으로 채운다.
 * 화면을 떠난 뒤 늦게 온 응답은 버린다. 다시 읽으려면 돌려받은 함수를 부른다.
 *
 * @param {HTMLElement} box
 * @param {object} opts
 * @param {() => Promise<any>} opts.load
 * @param {(data: any) => Node|Node[]|null} opts.render
 * @param {(data: any) => boolean} [opts.isEmpty]
 * @param {() => Node} [opts.empty]
 * @param {number} [opts.lines] 스켈레톤 줄 수
 * @param {string} [opts.unavailableTitle] 서버에 없는 기능일 때 보여 줄 문장
 * @param {boolean} [opts.missingIs404] 계정 전체 경로라 404면 모두 "없는 기능"으로 볼 때
 * @param {(err: any) => Node|null} [opts.onUnavailable] 없는 기능일 때 대신 그릴 것
 */
export function loadInto(box, opts) {
  let token = 0;
  const run = async () => {
    token += 1;
    const mine = token;
    setChildren(box, skeleton(opts.lines ?? 3));
    box.setAttribute("aria-busy", "true");
    let data;
    try {
      data = await opts.load();
    } catch (err) {
      if (mine !== token || !box.isConnected || isAbort(err)) return;
      box.removeAttribute("aria-busy");
      if (isUnavailable(err) || (opts.missingIs404 && err.status === 404)) {
        setChildren(box, opts.onUnavailable ? opts.onUnavailable(err) : unavailable(opts.unavailableTitle));
      } else if (err.status === 401) {
        setChildren(box, null); // 로그인 화면으로 넘어간다
      } else {
        setChildren(box, errorState(describeError(err), run));
      }
      return;
    }
    if (mine !== token || !box.isConnected) return;
    box.removeAttribute("aria-busy");
    if (opts.isEmpty && opts.isEmpty(data)) setChildren(box, opts.empty ? opts.empty(data) : emptyState("아직 아무것도 없어요"));
    else setChildren(box, opts.render(data));
    // 화면 제목이 이제야 그려졌다면(상세 화면 등) 포커스를 제목으로 옮긴다 — 아직 아무 데도 가 있지 않을 때만
    const active = document.activeElement;
    if (!active || active === document.body || active.id === "main") {
      box.querySelector("h1[tabindex]")?.focus({ preventScroll: true });
    }
  };
  run();
  return run;
}
