// 첫 화면(로그아웃 상태). 2초 안에 가치가 전달되는 예시 + 약속 세 가지 + 로그인·가입.
// 가입 폼은 서버 설정(/v1/auth/config)에 따라 달라진다: 가입 닫힘이면 숨기고, 초대 코드 칸, 동의 체크,
// 승인제 안내, 켜진 소셜 로그인(Google·Kakao) 버튼. 가입은 아이디 또는 이메일로 한다.

import { describeError } from "../lib/errors.js";
import { login, register, signupEmail } from "../session.js";
import { store } from "../store.js";
import { busy, h, setChildren, uid } from "../ui/dom.js";

// 가입 이름 규칙(서버 auth_service와 같다): 영문·숫자·_·- 3~32자. 한글은 받지 않는다.
const USERNAME_RULE = /^[A-Za-z0-9_-]{3,32}$/;
const EMAIL_RULE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

// 소셜 로그인이 실패해 돌아왔을 때(/?auth_error=<code>) 첫 화면에 보여 줄 안내
const AUTH_ERROR_TEXT = {
  oauth_denied: "로그인을 취소했어요.",
  oauth_failed: "소셜 로그인을 끝내지 못했어요. 다시 시도해 주세요.",
  signup_closed: "지금은 이 방법으로 새로 가입할 수 없어요. 이미 계정이 있으면 그 계정으로 로그인해 주세요.",
  account_suspended: "정지된 계정이에요. 관리자에게 문의해 주세요.",
};
const SOCIAL = [
  ["google", "Google로 계속하기"],
  ["kakao", "카카오로 계속하기"],
];

/** 주소의 auth_error를 읽어 안내로 바꾸고, 새로 고쳐도 다시 뜨지 않게 주소에서 지운다. */
export function takeAuthError(location = window.location, history = window.history) {
  const params = new URLSearchParams(location.search);
  const code = params.get("auth_error");
  if (!code) return null;
  params.delete("auth_error");
  const rest = params.toString();
  try {
    history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
  } catch {
    /* 주소를 못 바꿔도 안내는 보여 준다 */
  }
  return { text: AUTH_ERROR_TEXT[code] || AUTH_ERROR_TEXT.oauth_failed, tone: "warn" };
}

// 예시는 꾸민 것이다(실제 인물·대화가 아님). 같은 초안이 상대에 따라 달라지고 날짜·약속은 그대로 남는 모습.
const EXAMPLE = {
  draft: "이번 주 금요일은 안 되고 다음 주 수요일까지 보낼게요",
  results: [
    {
      who: "팀장님에게",
      parts: ["팀장님, ", ["이번 주 금요일"], "은 어렵고 ", ["다음 주 수요일"], "까지 보내 드리겠습니다."],
    },
    {
      who: "친구에게",
      parts: [["이번 주 금요일"], "은 안 되고 ", ["다음 주 수요일"], "까지 보내 줄게!"],
    },
  ],
  kept: ["이번 주 금요일", "다음 주 수요일", "보내겠다는 약속"],
};

function exampleNode() {
  return h(
    "figure",
    { class: "example", "aria-label": "예시: 같은 초안이 상대에 따라 달라지는 모습" },
    h(
      "div",
      { class: "example-draft" },
      h("span", { class: "example-label" }, "내 초안"),
      h("p", null, EXAMPLE.draft),
    ),
    h(
      "div",
      { class: "example-results" },
      EXAMPLE.results.map((r) =>
        h(
          "div",
          { class: "example-result" },
          h("span", { class: "example-label" }, r.who),
          h(
            "p",
            null,
            r.parts.map((part) => (Array.isArray(part) ? h("mark", null, part[0]) : part)),
          ),
        ),
      ),
    ),
    h(
      "figcaption",
      { class: "example-kept" },
      h("span", { class: "kept-label" }, "말투만 바뀌고 그대로 남은 것"),
      EXAMPLE.kept.map((k) => h("span", { class: "tag tag-ok" }, `✓ ${k}`)),
    ),
  );
}

function promisesNode() {
  const items = [
    ["사실은 그대로", "날짜·숫자·약속·가능/불가는 바꾸지 않아요. 바뀐 문장은 추천하지 않고 초안을 그대로 둬요."],
    ["내 말투로", "상대마다 정해 둔 말투와 내가 실제로 썼던 표현을 따라요."],
    ["자동으로 보내지 않아요", "복사까지만 해요. 전송은 직접 합니다."],
  ];
  return h(
    "ul",
    { class: "promises" },
    items.map(([title, body]) => h("li", null, h("strong", null, title), h("span", null, body))),
  );
}

/** 켜진 소셜 로그인 버튼. 서버가 제공자로 보냈다가 이 화면으로 돌려보낸다(실패하면 ?auth_error=). */
function socialNode(config) {
  const enabled = SOCIAL.filter(([name]) => config[name] === true);
  if (!enabled.length) return null;
  const next = encodeURIComponent(window.location.pathname || "/");
  return h(
    "div",
    { class: "auth-social" },
    h("p", { class: "auth-or" }, "또는"),
    enabled.map(([name, label]) =>
      h("a", { class: "btn btn-block", href: `/v1/auth/oauth/${name}/start?next=${next}` }, label),
    ),
  );
}

function authCard() {
  const config = store.config;
  let mode = "login";
  let by = "username"; // 가입 방법: "username" | "email"
  const card = h("section", { class: "card auth-card", "aria-labelledby": "auth-title" });

  function render() {
    const isRegister = mode === "register";
    const byEmail = isRegister && by === "email";
    const userId = uid("user");
    const passId = uid("pass");
    const pass2Id = uid("pass2");
    const inviteId = uid("invite");
    const consentId = uid("consent");
    const errorEl = h("p", { class: "form-error", role: "alert", hidden: true });
    const userEl = h("input", {
      id: userId,
      class: "input",
      type: byEmail ? "email" : "text",
      name: byEmail ? "email" : "username",
      autocomplete: byEmail ? "email" : "username",
      autocapitalize: "none",
      spellcheck: "false",
      required: true,
      minlength: 3,
      // 로그인은 아이디 또는 이메일을 받는다(이메일은 254자까지)
      maxlength: isRegister && !byEmail ? 32 : 254,
    });
    const passEl = h("input", {
      id: passId,
      class: "input",
      type: "password",
      name: "password",
      autocomplete: isRegister ? "new-password" : "current-password",
      required: true,
      maxlength: 128,
    });
    const pass2El = isRegister
      ? h("input", { id: pass2Id, class: "input", type: "password", autocomplete: "new-password", required: true, maxlength: 128 })
      : null;
    const inviteEl =
      isRegister && config.invite_required
        ? h("input", { id: inviteId, class: "input", type: "text", autocomplete: "off", spellcheck: "false", required: true, maxlength: 200 })
        : null;
    const consentEl = isRegister && config.require_consent ? h("input", { id: consentId, type: "checkbox", required: true }) : null;
    const submit = h("button", { type: "submit", class: "btn btn-primary btn-lg btn-block" }, isRegister ? "가입하고 시작하기" : "로그인");

    const fail = (message, focusEl) => {
      errorEl.textContent = message;
      errorEl.hidden = false;
      if (focusEl) focusEl.focus();
    };

    const onSubmit = async (e) => {
      e.preventDefault();
      errorEl.hidden = true;
      const username = userEl.value.trim();
      const password = passEl.value;
      if (byEmail) {
        if (!EMAIL_RULE.test(username)) return fail("이메일 주소를 다시 확인해 주세요.", userEl);
      } else if (username.length < 3) return fail("아이디는 3자 이상이어야 해요.", userEl);
      // 가입할 때만 모양을 본다(서버 규칙과 같다). 로그인은 예전에 만든 이름도 그대로 보내 서버가 판단하게 둔다.
      if (isRegister && !byEmail && !USERNAME_RULE.test(username)) {
        return fail("아이디는 영문·숫자·밑줄(_)·붙임표(-)만 3~32자로 쓸 수 있어요.", userEl);
      }
      if (!password) return fail("비밀번호를 입력해 주세요.", passEl);
      if (isRegister) {
        if (password.length < 8) return fail("비밀번호는 8자 이상이어야 해요.", passEl);
        if (pass2El.value !== password) return fail("비밀번호 확인이 서로 달라요.", pass2El);
        if (inviteEl && !inviteEl.value.trim()) return fail("초대 코드를 입력해 주세요.", inviteEl);
        if (consentEl && !consentEl.checked) return fail("안내를 확인하고 동의해야 가입할 수 있어요.", consentEl);
      }
      return busy(submit, isRegister ? "가입하는 중…" : "로그인하는 중…", async () => {
        try {
          if (isRegister) {
            const payload = byEmail ? { email: username, password } : { username, password };
            if (inviteEl) payload.invite_code = inviteEl.value.trim();
            if (consentEl) payload.consent = true;
            await (byEmail ? signupEmail(payload) : register(payload));
          } else {
            await login(username, password);
          }
        } catch (err) {
          // 오류는 폼 안에: 잘못된 비밀번호(401), 잠금(429), 이미 있는 이름(409), 초대 코드(403), 동의(422) 등
          let message = describeError(err);
          if (err.status === 401) message = "아이디나 비밀번호가 맞지 않아요.";
          if (err.status === 409) {
            message = byEmail ? "이미 가입된 이메일이에요. 로그인해 주세요." : "이미 쓰고 있는 아이디예요. 다른 아이디를 골라 주세요.";
          }
          if (err.code === "account_suspended") message = AUTH_ERROR_TEXT.account_suspended;
          if (err.code === "signup_closed") message = "지금은 새로 가입할 수 없어요.";
          if (err.code === "invalid_invite") message = "초대 코드가 맞지 않아요. 받은 코드를 다시 확인해 주세요.";
          if (err.code === "consent_required") message = "안내를 확인하고 동의해야 가입할 수 있어요.";
          fail(message, err.status === 401 ? passEl : err.status === 409 ? userEl : null);
          passEl.value = "";
          if (pass2El) pass2El.value = "";
        }
      });
    };

    const tabs = config.allow_register
      ? h(
          "div",
          { class: "segmented", role: "group", "aria-label": "로그인 또는 가입" },
          [
            ["login", "로그인"],
            ["register", "가입"],
          ].map(([value, label]) =>
            h(
              "button",
              {
                type: "button",
                class: "segmented-item",
                // 아래 제출 버튼("로그인")과 이름이 겹치지 않게 한다
                "aria-label": `${label} 화면`,
                "aria-pressed": String(mode === value),
                onClick: () => {
                  if (mode === value) return;
                  mode = value;
                  render();
                  card.querySelector("input")?.focus();
                },
              },
              label,
            ),
          ),
        )
      : null;

    setChildren(
      card,
      h("h2", { class: "auth-title", id: "auth-title" }, isRegister ? "가입" : "로그인"),
      tabs,
      store.notice ? h("p", { class: ["note", store.notice.tone ? `note-${store.notice.tone}` : ""], role: "status" }, store.notice.text) : null,
      h(
        "form",
        { class: "auth-form", novalidate: true, onSubmit },
        h(
          "div",
          { class: "field" },
          h("label", { for: userId }, byEmail ? "이메일" : isRegister ? "아이디" : "아이디 또는 이메일"),
          userEl,
          isRegister && !byEmail
            ? h("p", { class: "hint" }, "영문·숫자·_·- 로 3~32자(한글은 쓸 수 없어요). 대문자·소문자만 다른 아이디는 같은 아이디로 봐요.")
            : null,
          byEmail ? h("p", { class: "hint" }, "아이디는 이메일 앞부분으로 자동으로 정해져요. 로그인은 이메일로 하면 돼요.") : null,
          isRegister
            ? h(
                "button",
                {
                  type: "button",
                  class: "btn btn-quiet auth-switch",
                  onClick: () => {
                    by = byEmail ? "username" : "email";
                    render();
                    card.querySelector("input")?.focus();
                  },
                },
                byEmail ? "아이디로 가입하기" : "이메일로 가입하기",
              )
            : null,
        ),
        h(
          "div",
          { class: "field" },
          h("label", { for: passId }, "비밀번호"),
          passEl,
          isRegister ? h("p", { class: "hint" }, "8자 이상. 잊으면 스스로 되찾을 수 없으니 잘 적어 두세요.") : null,
        ),
        pass2El ? h("div", { class: "field" }, h("label", { for: pass2Id }, "비밀번호 확인"), pass2El) : null,
        inviteEl
          ? h(
              "div",
              { class: "field" },
              h("label", { for: inviteId }, "초대 코드"),
              inviteEl,
              h("p", { class: "hint" }, "지금은 초대받은 분만 가입할 수 있어요."),
            )
          : null,
        consentEl
          ? h(
              "div",
              { class: "check-row" },
              consentEl,
              h(
                "label",
                { for: consentId },
                h("a", { href: "/privacy", target: "_blank", rel: "noopener" }, "개인정보 처리방침"),
                "와 ",
                h("a", { href: "/terms", target: "_blank", rel: "noopener" }, "이용약관"),
                "를 읽었고 동의해요.",
              ),
            )
          : null,
        isRegister && config.requires_approval
          ? h("p", { class: "note", role: "note" }, "가입한 뒤 관리자가 승인하면 쓸 수 있어요.")
          : null,
        errorEl,
        submit,
      ),
      socialNode(config),
      !config.allow_register ? h("p", { class: "hint" }, "지금은 새로 가입할 수 없어요. 이미 계정이 있으면 로그인해 주세요.") : null,
      isRegister && !config.require_consent
        ? h(
            "p",
            { class: "hint" },
            "가입 전에 ",
            h("a", { href: "/privacy", target: "_blank", rel: "noopener" }, "개인정보 처리방침"),
            "와 ",
            h("a", { href: "/terms", target: "_blank", rel: "noopener" }, "이용약관"),
            "를 읽어 보세요.",
          )
        : null,
      store.config.error ? h("p", { class: "hint" }, "서버 설정을 읽지 못했어요. 가입이 안 되면 잠시 뒤 다시 시도해 주세요.") : null,
    );
    store.notice = null; // 한 번만 보여 준다
  }

  render();
  return card;
}

export function mountLanding(root) {
  document.title = "Persona_ize — 사실은 그대로, 말투만 상대에 맞게";
  if (!store.notice) store.notice = takeAuthError();
  root.replaceChildren(
    h(
      "div",
      { class: "landing" },
      h(
        "header",
        { class: "landing-top" },
        h("span", { class: "brand" }, "Persona_ize"),
        h(
          "nav",
          { class: "landing-links", "aria-label": "안내 문서" },
          h("a", { href: "/privacy" }, "개인정보 처리방침"),
          h("a", { href: "/terms" }, "이용약관"),
        ),
      ),
      h(
        "main",
        { class: "landing-main", id: "main" },
        h(
          "div",
          { class: "landing-pitch" },
          h("h1", { class: "landing-title" }, "사실은 그대로, 말투만 상대에 맞게"),
          h("p", { class: "landing-lead" }, "초안을 쓰면 상대와 상황에 맞는 내 말투로 다듬어요. 날짜·약속 같은 사실은 바꾸지 않아요."),
          exampleNode(),
          promisesNode(),
        ),
        authCard(),
      ),
    ),
  );
  return () => {};
}
