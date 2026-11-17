// 승인 대기 화면: 로그인은 됐지만 관리자가 아직 승인하지 않은 계정(status = pending).
// 승인 전에는 /v1/auth/* 밖의 API가 모두 403이라 앱 화면을 열지 않고 여기서 기다리게 한다.

import { describeError } from "../lib/errors.js";
import { boot, logout } from "../session.js";
import { store } from "../store.js";
import { busy, h } from "../ui/dom.js";

export function mountPending(root) {
  document.title = "승인을 기다리는 중 — Persona_ize";
  const stateEl = h("p", { class: "hint", role: "status", hidden: true });
  const title = h("h1", { class: "page-title", tabIndex: -1 }, "관리자 승인을 기다리고 있어요");

  const recheck = (e) =>
    busy(e.currentTarget, "확인하는 중…", async () => {
      stateEl.hidden = true;
      await boot(); // /v1/auth/me를 다시 읽는다
      if (store.me && !store.isPending()) {
        store.emit("session"); // 승인됐다 → 앱 화면으로
        return;
      }
      if (!store.me) {
        store.emit("session"); // 로그아웃됐거나 서버가 답하지 않는다 → 첫 화면·오류 화면
        return;
      }
      stateEl.textContent = "아직 승인 전이에요. 조금 뒤에 다시 확인해 주세요.";
      stateEl.hidden = false;
    }).catch((err) => {
      stateEl.textContent = describeError(err, "확인하지 못했어요. 잠시 뒤 다시 시도해 주세요.");
      stateEl.hidden = false;
    });

  root.replaceChildren(
    h(
      "div",
      { class: "landing" },
      h("header", { class: "landing-top" }, h("span", { class: "brand" }, "Persona_ize")),
      h(
        "main",
        { class: "consent-main", id: "main" },
        h(
          "section",
          { class: "card consent-card" },
          title,
          h(
            "p",
            { class: "page-lead" },
            `${store.me?.email || store.me?.username || ""} 계정으로 가입했어요. 관리자가 승인하면 바로 쓸 수 있어요.`,
          ),
          h("p", null, "승인됐다는 알림은 따로 가지 않아요. 아래 버튼으로 다시 확인해 주세요."),
          stateEl,
          h(
            "div",
            { class: "form-actions" },
            h("button", { type: "button", class: "btn btn-primary", onClick: recheck }, "다시 확인"),
            h("button", { type: "button", class: "btn", onClick: () => logout() }, "로그아웃"),
          ),
        ),
      ),
    ),
  );
  title.focus({ preventScroll: true });
  return () => {};
}
