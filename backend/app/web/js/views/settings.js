// 설정(#/settings): 비밀번호 변경, 로그인된 기기, 화면 테마, 단축키 안내, 동의 정보·문서, 오늘 사용량·한도.

import { api } from "../api.js";
import { describeError } from "../lib/errors.js";
import { formatDateTime, formatNumber, isMacLike } from "../lib/format.js";
import { usageView } from "../lib/model.js";
import { refreshUsage } from "../session.js";
import { getTheme, setTheme, store } from "../store.js";
import { busy, h, uid } from "../ui/dom.js";
import { UNAVAILABLE_TOAST, emptyState, kbd, loadInto, meter, note, pageHead, tag } from "../ui/parts.js";
import { toast } from "../ui/toast.js";

function passwordSection() {
  const curId = uid("cur");
  const newId = uid("new");
  const againId = uid("again");
  const curEl = h("input", { id: curId, class: "input", type: "password", autocomplete: "current-password", maxlength: 128, required: true });
  const newEl = h("input", { id: newId, class: "input", type: "password", autocomplete: "new-password", maxlength: 128, required: true });
  const againEl = h("input", { id: againId, class: "input", type: "password", autocomplete: "new-password", maxlength: 128, required: true });
  const errorEl = h("p", { class: "form-error", role: "alert", hidden: true });
  const doneEl = h("p", { class: "form-ok", role: "status", hidden: true });
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, "비밀번호 바꾸기");

  const fail = (message, el) => {
    errorEl.textContent = message;
    errorEl.hidden = false;
    if (el) el.focus();
  };

  const onSubmit = async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    doneEl.hidden = true;
    if (!curEl.value) return fail("지금 쓰는 비밀번호를 입력해 주세요.", curEl);
    if (newEl.value.length < 8) return fail("새 비밀번호는 8자 이상이어야 해요.", newEl);
    if (newEl.value !== againEl.value) return fail("새 비밀번호 확인이 서로 달라요.", againEl);
    if (newEl.value === curEl.value) return fail("새 비밀번호가 지금 비밀번호와 같아요.", newEl);
    return busy(submit, "바꾸는 중…", async () => {
      try {
        const out = await api.changePassword(curEl.value, newEl.value);
        const n = Number(out?.revoked_sessions) || 0;
        doneEl.textContent = n > 0 ? `비밀번호를 바꿨어요. 다른 기기 ${formatNumber(n)}곳에서는 로그아웃됐어요.` : "비밀번호를 바꿨어요.";
        doneEl.hidden = false;
        curEl.value = "";
        newEl.value = "";
        againEl.value = "";
      } catch (err) {
        // 지금 비밀번호가 틀린 경우(401)는 로그아웃시키지 않고 이 칸에서 알려 준다
        if (err.status === 401) fail("지금 비밀번호가 맞지 않아요.", curEl);
        else fail(describeError(err, "바꾸지 못했어요. 잠시 뒤 다시 시도해 주세요."), null);
      }
    });
  };

  return h(
    "section",
    { class: "card section" },
    h("h2", { class: "section-title" }, "비밀번호"),
    h("p", { class: "section-lead" }, "바꾸면 이 기기만 남기고 다른 기기에서는 모두 로그아웃돼요."),
    h(
      "form",
      { class: "stack-form", novalidate: true, onSubmit },
      // 비밀번호 관리자가 어느 계정인지 알 수 있게 아이디를 함께 둔다
      h("input", {
        type: "text",
        class: "sr-only",
        autocomplete: "username",
        value: store.me?.username || "",
        readOnly: true,
        tabIndex: -1,
        "aria-hidden": "true",
        "aria-label": "아이디",
      }),
      h("div", { class: "field" }, h("label", { for: curId }, "지금 비밀번호"), curEl),
      h("div", { class: "field" }, h("label", { for: newId }, "새 비밀번호"), newEl, h("p", { class: "hint" }, "8자 이상")),
      h("div", { class: "field" }, h("label", { for: againId }, "새 비밀번호 확인"), againEl),
      errorEl,
      doneEl,
      h("div", { class: "form-actions" }, submit),
    ),
    note("이메일을 받지 않아서 비밀번호를 잊으면 스스로 되찾을 수 없어요. 그때는 운영자에게 재설정을 요청해야 해요."),
  );
}

function sessionsSection() {
  const box = h("div");
  const titleEl = h("h2", { class: "section-title", tabIndex: -1 }, "로그인된 기기");
  const reload = loadInto(box, {
    lines: 2,
    load: () => api.sessions(),
    missingIs404: true,
    unavailableTitle: "로그인된 기기 목록은 지금 볼 수 없어요",
    isEmpty: (list) => !Array.isArray(list) || !list.length,
    empty: () => emptyState("로그인된 기기가 없어요"),
    render: (list) => {
      const others = list.filter((s) => !s.current).length;
      return [
        h(
          "ul",
          { class: "session-list" },
          list.map((s) =>
            h(
              "li",
              { class: "session" },
              h("span", null, `${formatDateTime(s.created_at)}에 로그인`),
              s.current ? tag("지금 이 기기", "accent") : null,
              h("span", { class: "muted" }, ` · ${formatDateTime(s.expires_at)}까지 유지`),
            ),
          ),
        ),
        h(
          "div",
          { class: "form-actions" },
          h(
            "button",
            {
              type: "button",
              class: "btn",
              disabled: others === 0,
              onClick: async (e) => {
                await busy(e.currentTarget, "로그아웃하는 중…", async () => {
                  try {
                    const out = await api.logoutAll();
                    toast(`다른 기기 ${formatNumber(out?.revoked_sessions ?? 0)}곳에서 로그아웃했어요.`, { kind: "ok" });
                    reload();
                    titleEl.focus(); // 목록을 다시 그리면 누른 버튼이 사라지므로 구획 제목으로
                  } catch (err) {
                    if (err.status === 401) return;
                    toast(err.unavailable ? UNAVAILABLE_TOAST : describeError(err, "로그아웃하지 못했어요."), { kind: err.unavailable ? "warn" : "error" });
                  }
                });
              },
            },
            "다른 기기 모두 로그아웃",
          ),
          others === 0 ? h("span", { class: "muted" }, "다른 기기에는 로그인돼 있지 않아요.") : null,
        ),
      ];
    },
  });
  return h(
    "section",
    { class: "card section" },
    titleEl,
    h("p", { class: "section-lead" }, "이 계정으로 로그인된 곳이에요. 기기 이름은 저장하지 않아 로그인한 시각으로 보여 줘요."),
    box,
  );
}

function themeSection() {
  const name = uid("theme");
  const options = [
    { value: "system", label: "시스템 설정 따르기" },
    { value: "light", label: "밝게" },
    { value: "dark", label: "어둡게" },
  ];
  const current = getTheme();
  return h(
    "section",
    { class: "card section" },
    h("h2", { class: "section-title" }, "화면 테마"),
    h(
      "fieldset",
      { class: "field" },
      h("legend", { class: "sr-only" }, "화면 테마"),
      h(
        "div",
        { class: "seg seg-3" },
        options.map((o) => {
          const inputId = uid("t");
          return [
            h("input", {
              type: "radio",
              class: "seg-input",
              id: inputId,
              name,
              value: o.value,
              checked: o.value === current,
              onChange: () => setTheme(o.value),
            }),
            h("label", { for: inputId, class: "seg-label" }, h("span", { class: "seg-text" }, o.label)),
          ];
        }),
      ),
      h("p", { class: "hint" }, "이 브라우저에만 저장돼요."),
    ),
  );
}

function shortcutsSection() {
  const mod = isMacLike(navigator.platform || navigator.userAgent) ? "⌘" : "Ctrl";
  const rows = [
    [[mod, "Enter"], "말투 맞추기 (초안을 쓰는 중에도)"],
    [["Enter"], "추천 문장 복사"],
    [["R"], "다시 만들기"],
    [["1"], "더 짧게"],
    [["2"], "더 공손하게"],
    [["3"], "더 편하게"],
    [["Esc"], "결과 닫기 · 초안 그대로"],
  ];
  return h(
    "section",
    { class: "card section" },
    h("h2", { class: "section-title" }, "단축키"),
    h("p", { class: "section-lead" }, "첨삭 화면에서 써요. 글자를 입력하는 중에는 Enter·R·숫자 키가 그대로 글자로 들어가요."),
    h(
      "table",
      { class: "table table-compact" },
      h("caption", { class: "sr-only" }, "첨삭 화면 단축키"),
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "키"), h("th", { scope: "col" }, "하는 일"))),
      h(
        "tbody",
        null,
        rows.map(([keys, what]) => h("tr", null, h("td", null, kbd(...keys)), h("td", null, what))),
      ),
    ),
  );
}

function usageSection() {
  const box = h("div");
  loadInto(box, {
    lines: 2,
    load: async () => (await refreshUsage()) || api.usage(),
    render: (usage) => {
      const u = usageView(usage);
      if (!u) return emptyState("사용량을 읽지 못했어요");
      return [
        h(
          "div",
          { class: "usage-block" },
          h("p", { class: "usage-line" }, h("strong", null, "첨삭 요청"), ` ${formatNumber(u.rewrites)} / ${formatNumber(u.rewriteLimit)}회`, u.exhausted ? [" ", tag("한도 도달", "warn")] : null),
          meter(u.rewrites, u.rewriteLimit, "오늘 첨삭 요청"),
        ),
        h(
          "div",
          { class: "usage-block" },
          h("p", { class: "usage-line" }, h("strong", null, "모델 사용량(토큰)"), ` ${formatNumber(u.tokens)} / ${formatNumber(u.tokenLimit)}`),
          meter(u.tokens, u.tokenLimit, "오늘 모델 사용량"),
        ),
        u.showGraph
          ? h(
              "div",
              { class: "usage-block" },
              h(
                "p",
                { class: "usage-line" },
                h("strong", null, "지난 약속·사건 정리"),
                ` 메시지 ${formatNumber(u.graphIngested)}개`,
                u.graphCap ? ` / ${formatNumber(u.graphCap)}개` : "",
              ),
              u.graphCap ? meter(u.graphIngested, u.graphCap, "사건 정리에 쓴 메시지") : null,
            )
          : null,
        note("한도는 한국 시간 자정에 새로 시작돼요. 한도에 닿으면 그날은 더 만들 수 없어요."),
      ];
    },
  });
  return h("section", { class: "card section", id: "usage" }, h("h2", { class: "section-title" }, "오늘 사용량"), box);
}

function consentSection() {
  const me = store.me || {};
  const config = store.config;
  let status;
  if (me.consented_at) {
    status = [
      `${formatDateTime(me.consented_at)}에 동의했어요`,
      me.consent_version ? ` (문서 버전 ${me.consent_version})` : "",
      me.consent_current === false ? [" ", tag("문서가 바뀌었어요", "warn")] : null,
    ];
  } else if (!config.loaded) {
    status = "동의 정보는 지금 확인할 수 없어요.";
  } else if (!config.require_consent) {
    status = "이 서버는 가입할 때 따로 동의를 받지 않아요.";
  } else {
    status = "동의 기록이 없어요.";
  }
  return h(
    "section",
    { class: "card section" },
    h("h2", { class: "section-title" }, "안내 문서와 동의"),
    h("p", null, status),
    h(
      "ul",
      { class: "plain-list" },
      h("li", null, h("a", { href: "/privacy" }, "개인정보 처리방침"), " — 무엇을 저장하고 무엇이 밖으로 나가는지"),
      h("li", null, h("a", { href: "/terms" }, "이용약관"), " — 이 서비스가 하는 일과 하지 않는 일"),
    ),
  );
}

export function mountSettings(root) {
  const me = store.me || {};
  root.replaceChildren(
    pageHead("설정"),
    h(
      "section",
      { class: "card section" },
      h("h2", { class: "section-title" }, "계정"),
      h("p", null, "아이디 ", h("strong", null, me.username || ""), me.is_admin ? [" ", tag("관리자", "accent")] : null),
    ),
    usageSection(),
    passwordSection(),
    sessionsSection(),
    themeSection(),
    shortcutsSection(),
    consentSection(),
  );
  return () => {};
}
