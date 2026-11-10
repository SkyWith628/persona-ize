// 동의 화면: 로그인했는데 서버가 동의를 요구하고(require_consent) 지금 버전에 동의한 기록이 없을 때 먼저 받는다.

import { describeError } from "../lib/errors.js";
import { acceptConsent, logout } from "../session.js";
import { store } from "../store.js";
import { busy, h, uid } from "../ui/dom.js";

export function mountConsent(root) {
  document.title = "시작하기 전에 — Persona_ize";
  const checkId = uid("consent");
  const check = h("input", { id: checkId, type: "checkbox" });
  const errorEl = h("p", { class: "form-error", role: "alert", hidden: true });
  const submit = h("button", { type: "submit", class: "btn btn-primary btn-lg" }, "동의하고 시작하기");

  const onSubmit = async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    if (!check.checked) {
      errorEl.textContent = "안내를 확인했다고 체크해 주세요.";
      errorEl.hidden = false;
      check.focus();
      return;
    }
    await busy(submit, "기록하는 중…", async () => {
      try {
        await acceptConsent();
      } catch (err) {
        if (err.status === 401) return;
        errorEl.textContent = err.unavailable
          ? "지금은 동의를 기록할 수 없어요. 잠시 뒤 다시 시도해 주세요."
          : describeError(err, "동의를 기록하지 못했어요. 잠시 뒤 다시 시도해 주세요.");
        errorEl.hidden = false;
      }
    });
  };

  root.replaceChildren(
    h(
      "div",
      { class: "landing" },
      h("header", { class: "landing-top" }, h("span", { class: "brand" }, "Persona_ize")),
      h(
        "main",
        { class: "consent-main", id: "main" },
        h(
          "form",
          { class: "card consent-card", novalidate: true, onSubmit },
          h("h1", { class: "page-title", tabIndex: -1 }, "시작하기 전에 확인해 주세요"),
          h(
            "p",
            { class: "page-lead" },
            store.me?.consent_version
              ? "안내 문서가 바뀌었어요. 다시 확인하고 동의해 주세요."
              : `${store.me?.username || ""} 님, 쓰기 전에 아래 내용을 확인해 주세요.`,
          ),
          h(
            "ul",
            { class: "consent-list" },
            h("li", null, h("strong", null, "저장하는 것"), " — 계정, 상대와 말투 설정, 올린 대화와 그 대화로 만든 검색용 자료, 첨삭 결과와 피드백, 사용량."),
            h(
              "li",
              null,
              h("strong", null, "밖으로 나가는 것"),
              " — 첨삭할 때 초안·상황·말투 설정·관련된 내 과거 표현이 Anthropic의 Claude API로 전송돼요. 대화를 올리면 지난 약속·사건을 정리하려고 그 메시지도 전송될 수 있어요.",
            ),
            h("li", null, h("strong", null, "지우는 방법"), " — 대화방·상대·첨삭 이력·계정을 언제든 직접 지울 수 있어요."),
            h("li", null, h("strong", null, "자동으로 보내지 않아요"), " — 복사까지만 해요. 전송은 직접 합니다."),
          ),
          h(
            "p",
            null,
            "자세한 내용: ",
            h("a", { href: "/privacy", target: "_blank", rel: "noopener" }, "개인정보 안내"),
            " · ",
            h("a", { href: "/terms", target: "_blank", rel: "noopener" }, "이용 안내"),
            store.config.consent_version ? h("span", { class: "muted" }, ` (문서 버전 ${store.config.consent_version})`) : null,
          ),
          h("div", { class: "check-row" }, check, h("label", { for: checkId }, "위 내용과 두 안내 문서를 확인했고 동의해요.")),
          errorEl,
          h(
            "div",
            { class: "form-actions" },
            submit,
            h("button", { type: "button", class: "btn btn-quiet", onClick: () => logout() }, "동의하지 않고 로그아웃"),
          ),
        ),
      ),
    ),
  );
  root.querySelector("h1")?.focus();
  return () => {};
}
