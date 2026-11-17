// 첫 화면(로그아웃 상태). 2초 안에 가치가 전달되는 예시 + 약속 세 가지 + 로그인·가입.
// 가입 폼은 서버 설정(/v1/auth/config)에 따라 달라진다: 가입 닫힘이면 숨기고, 초대 코드 칸, 동의 체크.
// 서버가 /v1/auth/providers를 주면 이메일 가입과 소셜 로그인(Google·카카오) 버튼, 승인제 안내가 더해진다.
// 승인제 서버에서 가입하면(202) 세션이 없으므로 앱으로 들어가지 않고 '승인 대기' 안내를 보여 준다.

import { describeError } from "../lib/errors.js";
import { login, register, signup, startOAuth } from "../session.js";
import { store } from "../store.js";
import { busy, h, setChildren, uid } from "../ui/dom.js";

// 가입 이름 규칙(서버 auth_service와 같다): 영문·숫자·_·- 3~32자. 한글은 받지 않는다.
const USERNAME_RULE = /^[A-Za-z0-9_-]{3,32}$/;

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

// ---------- 로그인·가입 카드 ----------

// 소셜 로그인 제공자. 서버(/v1/auth/providers)가 켜져 있다고 한 것만 버튼을 낸다.
const SOCIAL = [
  { key: "google", label: "Google로 계속하기" },
  { key: "kakao", label: "카카오로 계속하기" },
];

/**
 * 서버 설정 → 카드가 무엇을 보여 줄지. 순수 함수(tests/landing.test.js).
 * config는 store.config: /v1/auth/config의 값 + providers(/v1/auth/providers의 응답, 못 읽었으면 null).
 * providers를 모르는 옛 서버에서는 이메일 가입·소셜 로그인 없이 예전 화면(아이디 로그인·가입)과 같다.
 */
export function authOptions(config) {
  const base = config && typeof config === "object" ? config : {};
  const p = base.providers && typeof base.providers === "object" ? base.providers : null;
  // 가입 정책은 /config 값을 먼저 쓰고(예전과 같다), 그쪽을 못 읽었을 때만 /providers 값을 쓴다
  const pick = (configKey, providerKey, fallback) =>
    base.loaded ? base[configKey] === true : p ? p[providerKey] === true : fallback;
  return {
    signupOpen: pick("allow_register", "signup_open", base.allow_register !== false),
    inviteRequired: pick("invite_required", "invite_required", base.invite_required === true),
    requireConsent: pick("require_consent", "require_consent", base.require_consent === true),
    requiresApproval: p?.requires_approval === true,
    emailSignup: p !== null,
    social: SOCIAL.filter((s) => p?.[s.key] === true).map((s) => s.key),
  };
}

/** 이메일 모양만 가볍게 본다(서버가 다시 검사한다): 공백 없이 @ 하나와 점이 있는 도메인, 254자 이하. */
export function looksLikeEmail(value) {
  const v = String(value ?? "");
  return v.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
}

/**
 * 로그인·가입·소셜 시작 요청의 오류 → 폼 안에 보여 줄 문장. mode: "login" | "signup"(이메일) | "register"(아이디) | "social".
 * 잘못된 비밀번호(401), 잠금(429), 이미 있는 아이디·이메일(409), 초대 코드(403), 동의(422), 승인 대기·정지(403).
 */
export function authErrorMessage(err, mode) {
  if (!err) return describeError(err);
  if (err.code === "account_pending") return "아직 승인되지 않은 계정이에요. 관리자가 승인한 뒤에 로그인할 수 있어요.";
  if (err.code === "account_suspended") return "정지된 계정이에요. 운영자에게 문의해 주세요.";
  if (err.code === "invalid_invite") return "초대 코드가 맞지 않아요. 받은 코드를 다시 확인해 주세요.";
  if (err.code === "consent_required") return "안내를 확인하고 동의해야 가입할 수 있어요.";
  if (err.status === 401) return "아이디(또는 이메일)나 비밀번호가 맞지 않아요.";
  if (err.status === 409) {
    return mode === "signup"
      ? "이미 가입된 이메일이에요. 로그인하거나 다른 이메일을 써 주세요."
      : "이미 쓰고 있는 아이디예요. 다른 아이디를 골라 주세요.";
  }
  if (mode === "social" && err.status === 404) return "이 로그인 방식은 지금 쓸 수 없어요.";
  if (err.status === 403 && err.code === "forbidden" && (mode === "signup" || mode === "register")) {
    return "지금은 새로 가입할 수 없어요. 이미 계정이 있으면 로그인해 주세요.";
  }
  return describeError(err);
}

function docsLinks(tail) {
  return [
    h("a", { href: "/privacy", target: "_blank", rel: "noopener" }, "개인정보 처리방침"),
    "와 ",
    h("a", { href: "/terms", target: "_blank", rel: "noopener" }, "이용약관"),
    tail,
  ];
}

function authCard() {
  const options = authOptions(store.config);
  const joinMode = options.emailSignup ? "signup" : "register"; // '가입'을 누르면 처음 보이는 폼
  // 소셜 로그인에서 "동의·초대 코드가 필요하다"고 돌아왔으면 그 값을 넣을 수 있는 가입 화면에서 시작한다
  const cameBackToJoin = ["invalid_invite", "consent_required"].includes(store.notice?.authError);
  let mode = cameBackToJoin && options.signupOpen ? joinMode : "login"; // "login" | "signup" | "register" | "pending"
  let pending = null; // 승인 대기 응답({username, email})
  const card = h("section", { class: "card auth-card", "aria-labelledby": "auth-title" });

  /** 승인제 서버에서 가입한 직후: 세션이 없으므로 앱으로 들어가지 않고 이 안내만 보여 준다. */
  function renderPending() {
    const title = h("h2", { class: "auth-title", id: "auth-title", tabIndex: -1 }, "승인을 기다리고 있어요");
    setChildren(
      card,
      title,
      h("p", { class: "note note-ok", role: "status" }, "가입 신청을 받았어요. 관리자가 승인하면 로그인할 수 있어요."),
      h(
        "dl",
        { class: "auth-facts" },
        h("div", null, h("dt", null, "아이디"), h("dd", null, pending.username || "–")),
        pending.email ? h("div", null, h("dt", null, "이메일"), h("dd", null, pending.email)) : null,
      ),
      h("p", { class: "hint" }, "승인되기 전에는 로그인되지 않아요. 승인된 뒤에는 위 아이디나 이메일 어느 쪽으로든 로그인할 수 있어요."),
      h(
        "button",
        {
          type: "button",
          class: "btn btn-block",
          onClick: () => {
            mode = "login";
            pending = null;
            render();
            card.querySelector("input")?.focus();
          },
        },
        "로그인 화면으로",
      ),
    );
    title.focus({ preventScroll: true });
  }

  function render() {
    if (mode === "pending") {
      renderPending();
      return;
    }
    const isSignup = mode === "signup"; // 이메일 가입
    const isRegister = mode === "register"; // 아이디 가입
    const isJoin = isSignup || isRegister;
    const userId = uid("user");
    const nameId = uid("name");
    const passId = uid("pass");
    const pass2Id = uid("pass2");
    const inviteId = uid("invite");
    const consentId = uid("consent");
    const errorEl = h("p", { class: "form-error", role: "alert", hidden: true });
    // 첫 칸: 로그인은 아이디 또는 이메일, 이메일 가입은 이메일, 아이디 가입은 아이디
    const userEl = isSignup
      ? h("input", {
          id: userId,
          class: "input",
          type: "email",
          name: "email",
          autocomplete: "email",
          autocapitalize: "none",
          spellcheck: "false",
          required: true,
          maxlength: 254,
        })
      : h("input", {
          id: userId,
          class: "input",
          type: "text",
          name: "username",
          autocomplete: "username",
          autocapitalize: "none",
          spellcheck: "false",
          required: true,
          minlength: 3,
          maxlength: isRegister ? 32 : 254,
        });
    const nameEl = isSignup
      ? h("input", { id: nameId, class: "input", type: "text", name: "display_name", autocomplete: "nickname", maxlength: 40 })
      : null;
    const passEl = h("input", {
      id: passId,
      class: "input",
      type: "password",
      name: "password",
      autocomplete: isJoin ? "new-password" : "current-password",
      required: true,
      maxlength: 128,
    });
    const pass2El = isJoin
      ? h("input", { id: pass2Id, class: "input", type: "password", autocomplete: "new-password", required: true, maxlength: 128 })
      : null;
    const inviteEl =
      isJoin && options.inviteRequired
        ? h("input", { id: inviteId, class: "input", type: "text", autocomplete: "off", spellcheck: "false", required: true, maxlength: 128 })
        : null;
    const consentEl = isJoin && options.requireConsent ? h("input", { id: consentId, type: "checkbox", required: true }) : null;
    const submit = h("button", { type: "submit", class: "btn btn-primary btn-lg btn-block" }, isJoin ? "가입하고 시작하기" : "로그인");

    const fail = (message, focusEl) => {
      errorEl.textContent = message;
      errorEl.hidden = false;
      if (focusEl) focusEl.focus();
    };

    /** 가입 화면의 초대 코드·동의 값. 빠졌으면 알리고 null을 돌려준다(폼 제출과 소셜 가입이 함께 쓴다). */
    const joinTerms = () => {
      const terms = { inviteCode: "", consent: false };
      if (inviteEl) {
        terms.inviteCode = inviteEl.value.trim();
        if (!terms.inviteCode) {
          fail("초대 코드를 입력해 주세요.", inviteEl);
          return null;
        }
      }
      if (consentEl) {
        if (!consentEl.checked) {
          fail("안내를 확인하고 동의해야 가입할 수 있어요.", consentEl);
          return null;
        }
        terms.consent = true;
      }
      return terms;
    };

    const onSubmit = async (e) => {
      e.preventDefault();
      errorEl.hidden = true;
      const username = userEl.value.trim();
      const password = passEl.value;
      if (isSignup) {
        if (!looksLikeEmail(username)) return fail("이메일 주소를 확인해 주세요. 예: name@example.com", userEl);
      } else if (username.length < 3) {
        return fail(isRegister ? "아이디는 3자 이상이어야 해요." : "아이디나 이메일을 입력해 주세요.", userEl);
      }
      // 가입할 때만 모양을 본다(서버 규칙과 같다). 로그인은 예전에 만든 이름도 그대로 보내 서버가 판단하게 둔다.
      if (isRegister && !USERNAME_RULE.test(username)) {
        return fail("아이디는 영문·숫자·밑줄(_)·붙임표(-)만 3~32자로 쓸 수 있어요.", userEl);
      }
      if (!password) return fail("비밀번호를 입력해 주세요.", passEl);
      let terms = null;
      if (isJoin) {
        if (password.length < 8) return fail("비밀번호는 8자 이상이어야 해요.", passEl);
        if (pass2El.value !== password) return fail("비밀번호 확인이 서로 달라요.", pass2El);
        terms = joinTerms();
        if (!terms) return undefined;
      }
      return busy(submit, isJoin ? "가입하는 중…" : "로그인하는 중…", async () => {
        try {
          if (!isJoin) {
            await login(username, password);
            return;
          }
          const payload = isSignup ? { email: username, password } : { username, password };
          if (isSignup && nameEl.value.trim()) payload.display_name = nameEl.value.trim();
          if (inviteEl) payload.invite_code = terms.inviteCode;
          if (consentEl) payload.consent = true;
          const out = await (isSignup ? signup(payload) : register(payload));
          // 승인제 서버(202): 세션이 없다 — 앱으로 들어가지 않고 승인 대기 안내를 보여 준다
          if (out.pending) {
            pending = { username: out.username, email: out.email };
            mode = "pending";
            render();
          }
        } catch (err) {
          // 오류는 폼 안에: 잘못된 비밀번호(401), 잠금(429), 이미 있는 이름(409), 초대 코드(403), 동의(422), 승인 대기·정지(403) 등
          fail(authErrorMessage(err, mode), err.status === 401 ? passEl : err.status === 409 ? userEl : null);
          passEl.value = "";
          if (pass2El) pass2El.value = "";
        }
      });
    };

    // 소셜 로그인: 서버에서 제공자 주소를 받아 그곳으로 간다. 가입 화면에서는 초대 코드·동의를 함께 보낸다.
    const onSocial = (provider, button) => {
      errorEl.hidden = true;
      const terms = isJoin ? joinTerms() : { inviteCode: "", consent: false };
      if (!terms) return undefined;
      return busy(button, "이동하는 중…", async () => {
        try {
          await startOAuth(provider, terms);
        } catch (err) {
          fail(authErrorMessage(err, "social"), null);
        }
      });
    };

    const tabs = options.signupOpen
      ? h(
          "div",
          { class: "segmented", role: "group", "aria-label": "로그인 또는 가입" },
          [
            ["login", "로그인"],
            ["join", "가입"],
          ].map(([value, label]) =>
            h(
              "button",
              {
                type: "button",
                class: "segmented-item",
                // 아래 제출 버튼("로그인")과 이름이 겹치지 않게 한다
                "aria-label": `${label} 화면`,
                "aria-pressed": String((value === "join") === isJoin),
                onClick: () => {
                  if ((value === "join") === isJoin) return;
                  mode = value === "join" ? joinMode : "login";
                  render();
                  card.querySelector("input")?.focus();
                },
              },
              label,
            ),
          ),
        )
      : null;

    // 이메일 가입 ↔ 아이디 가입 (이메일 가입을 아는 서버에서만 고를 수 있다)
    const switchJoin =
      isJoin && options.emailSignup
        ? h(
            "button",
            {
              type: "button",
              class: "link-button",
              onClick: () => {
                mode = isSignup ? "register" : "signup";
                render();
                card.querySelector("input")?.focus();
              },
            },
            isSignup ? "이메일 없이 아이디로 가입하기" : "이메일로 가입하기",
          )
        : null;

    const social = options.social.length
      ? h(
          "div",
          { class: "auth-social" },
          h("p", { class: "auth-or" }, "또는"),
          SOCIAL.filter((s) => options.social.includes(s.key)).map((s) =>
            h("button", { type: "button", class: "btn btn-block", onClick: (e) => onSocial(s.key, e.currentTarget) }, s.label),
          ),
          isJoin && (inviteEl || consentEl)
            ? h(
                "p",
                { class: "hint" },
                `소셜 계정으로 가입할 때도 위의 ${[inviteEl ? "초대 코드" : "", consentEl ? "동의" : ""].filter(Boolean).join("와 ")}가 쓰여요. `,
                isSignup ? "이메일과 비밀번호 칸은 비워 둬도 돼요." : "아이디와 비밀번호 칸은 비워 둬도 돼요.",
              )
            : null,
          !isJoin && options.signupOpen && (options.inviteRequired || options.requireConsent)
            ? h("p", { class: "hint" }, "소셜 계정으로 처음 오셨다면 ‘가입’ 화면에서 시작해 주세요.")
            : null,
        )
      : null;

    setChildren(
      card,
      h("h2", { class: "auth-title", id: "auth-title" }, isJoin ? "가입" : "로그인"),
      tabs,
      store.notice ? h("p", { class: ["note", store.notice.tone ? `note-${store.notice.tone}` : ""], role: "status" }, store.notice.text) : null,
      isJoin && options.requiresApproval
        ? h("p", { class: "note note-warn" }, "가입하면 관리자가 승인한 뒤에 로그인할 수 있어요.")
        : null,
      h(
        "form",
        { class: "auth-form", novalidate: true, onSubmit },
        h(
          "div",
          { class: "field" },
          h("label", { for: userId }, isSignup ? "이메일" : isRegister ? "아이디" : "아이디 또는 이메일"),
          userEl,
          isRegister
            ? h("p", { class: "hint" }, "영문·숫자·_·- 로 3~32자(한글은 쓸 수 없어요). 대문자·소문자만 다른 아이디는 같은 아이디로 봐요.")
            : null,
          isSignup ? h("p", { class: "hint" }, "아이디는 이메일 앞부분으로 자동으로 만들어져요. 로그인은 이메일로 하면 돼요.") : null,
        ),
        nameEl
          ? h("div", { class: "field" }, h("label", { for: nameId }, "표시 이름 (선택)"), nameEl, h("p", { class: "hint" }, "40자까지. 비워 둬도 돼요."))
          : null,
        h(
          "div",
          { class: "field" },
          h("label", { for: passId }, "비밀번호"),
          passEl,
          isRegister ? h("p", { class: "hint" }, "8자 이상. 이메일이 없어 잊으면 스스로 되찾을 수 없으니 잘 적어 두세요.") : null,
          isSignup ? h("p", { class: "hint" }, "8자 이상. 잊으면 운영자에게 재설정을 요청해야 하니 잘 적어 두세요.") : null,
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
        consentEl ? h("div", { class: "check-row" }, consentEl, h("label", { for: consentId }, docsLinks("를 읽었고 동의해요."))) : null,
        errorEl,
        submit,
      ),
      switchJoin,
      social,
      !options.signupOpen ? h("p", { class: "hint" }, "지금은 새로 가입할 수 없어요. 이미 계정이 있으면 로그인해 주세요.") : null,
      isJoin && !options.requireConsent ? h("p", { class: "hint" }, "가입 전에 ", docsLinks("를 읽어 보세요.")) : null,
      store.config.error ? h("p", { class: "hint" }, "서버 설정을 읽지 못했어요. 가입이 안 되면 잠시 뒤 다시 시도해 주세요.") : null,
    );
    store.notice = null; // 한 번만 보여 준다
  }

  render();
  return card;
}

export function mountLanding(root) {
  document.title = "Persona_ize — 사실은 그대로, 말투만 상대에 맞게";
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
