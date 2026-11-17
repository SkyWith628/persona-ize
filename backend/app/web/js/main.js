// Persona_ize PC 웹 앱의 시작점. 빌드 없는 ES 모듈 + 해시 라우팅(#/compose 등).
// 로그인 상태에 따라 첫 화면 → 승인 대기 → 동의 → 앱 셸(화면들) 중 하나를 그린다.

import { describeError } from "./lib/errors.js";
import { DEFAULT_ROUTE, buildHash, matchRoute } from "./lib/router.js";
import { boot, leave } from "./session.js";
import { store } from "./store.js";
import { busy, h } from "./ui/dom.js";
import { skeleton } from "./ui/parts.js";
import { mountCompose } from "./views/compose.js";
import { mountConsent } from "./views/consent.js";
import { mountContactDetail } from "./views/contact-detail.js";
import { mountContacts } from "./views/contacts.js";
import { mountData } from "./views/data.js";
import { mountHistory } from "./views/history.js";
import { mountImport } from "./views/import.js";
import { mountLanding } from "./views/landing.js";
import { mountPending } from "./views/pending.js";
import { mountSettings } from "./views/settings.js";
import { mountShell } from "./views/shell.js";

const VIEWS = {
  compose: mountCompose,
  contacts: mountContacts,
  contact: mountContactDetail,
  import: mountImport,
  history: mountHistory,
  data: mountData,
  settings: mountSettings,
};

const root = document.getElementById("root");
let mode = null; // "landing" | "pending" | "consent" | "app" | "error"
let shell = null;
let cleanup = null; // 지금 화면의 정리 함수

function unmountView() {
  if (typeof cleanup === "function") {
    try {
      cleanup();
    } catch (err) {
      console.error(err);
    }
  }
  cleanup = null;
}

function navigate(hash, { replace = false } = {}) {
  if (replace) window.location.replace(hash);
  else window.location.hash = hash;
}

function renderRoute() {
  if (mode !== "app" || !shell) return;
  const route = matchRoute(window.location.hash);
  if (!route) {
    navigate(buildHash(DEFAULT_ROUTE), { replace: true });
    return;
  }
  unmountView();
  // 뒤로 가기 등으로 화면이 바뀌면 열려 있던 확인 대화상자는 닫는다(이전 화면의 일을 이어서 하지 않게)
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  shell.setActive(route.nav);
  document.title = `${route.title} — Persona_ize`;
  shell.outlet.replaceChildren();
  shell.outlet.scrollTop = 0;
  window.scrollTo(0, 0);
  try {
    cleanup = VIEWS[route.name](shell.outlet, { route, navigate });
  } catch (err) {
    console.error(err);
    shell.outlet.replaceChildren(
      h(
        "div",
        { class: "state state-error", role: "alert" },
        h("p", { class: "state-title" }, "화면을 그리지 못했어요"),
        h("p", { class: "state-body" }, "새로 고친 뒤 다시 시도해 주세요."),
      ),
    );
  }
  // 화면이 스스로 포커스를 잡지 않았으면 제목으로 옮긴다(키보드·낭독기 사용자가 바뀐 화면을 알 수 있게)
  const active = document.activeElement;
  if (!active || active === document.body || !shell.outlet.contains(active)) {
    // 제목이 아직 없으면(내용을 불러오는 중) 본문 영역에 두었다가, 내용이 그려지면 제목으로 옮긴다(ui/parts.js loadInto)
    (shell.outlet.querySelector("h1") || shell.outlet).focus({ preventScroll: true });
  }
}

/**
 * 시작할 때 서버가 답하지 못해 로그인 여부를 알 수 없을 때의 화면.
 * 로그인 화면을 보여 주면 "로그아웃됐다"고 오해하므로, 까닭과 다시 시도만 보여 준다. 주소(#/…)는 그대로 둔다.
 */
function mountBootError(err) {
  document.title = "Persona_ize";
  const retry = h(
    "button",
    {
      type: "button",
      class: "btn btn-primary",
      onClick: (e) =>
        busy(e.currentTarget, "다시 확인하는 중…", async () => {
          await boot();
          render();
        }),
    },
    "다시 시도",
  );
  const title = h("h1", { class: "page-title", tabIndex: -1 }, "지금은 Persona_ize를 열 수 없어요");
  root.replaceChildren(
    h(
      "main",
      { class: "boot-error", id: "main" },
      title,
      h("p", { role: "alert" }, describeError(err)),
      h("p", { class: "muted" }, "서버가 답하지 않아 로그인 상태를 확인하지 못했어요. 로그인돼 있었다면 그대로예요."),
      h("div", { class: "form-actions" }, retry),
    ),
  );
  title.focus({ preventScroll: true });
  return () => {};
}

function render() {
  unmountView();
  if (shell) {
    shell.destroy();
    shell = null;
  }
  if (store.bootError && !store.me) {
    mode = "error";
    cleanup = mountBootError(store.bootError);
  } else if (!store.me) {
    mode = "landing";
    cleanup = mountLanding(root);
  } else if (store.isPending()) {
    mode = "pending";
    cleanup = mountPending(root);
  } else if (store.needsConsent()) {
    mode = "consent";
    cleanup = mountConsent(root);
  } else {
    mode = "app";
    shell = mountShell(root);
    renderRoute();
  }
}

async function start() {
  root.replaceChildren(h("div", { class: "boot" }, skeleton(3, "Persona_ize를 여는 중이에요")));
  await boot();
  store.on("session", render);
  store.on("unauthorized", () => {
    // 세션이 끝났다: 초안은 이 탭에 남겨 두고 로그인 화면으로
    if (!store.me) return;
    leave({ text: "로그인이 만료됐어요. 다시 로그인하면 쓰던 초안이 그대로 있어요.", tone: "warn" }, { forget: false });
  });
  window.addEventListener("hashchange", renderRoute);
  // "본문으로 건너뛰기": 해시(#main)를 바꾸면 라우터가 화면 이동으로 오해하므로 포커스만 옮긴다
  document.querySelector(".skip-link")?.addEventListener("click", (e) => {
    e.preventDefault();
    const main = document.getElementById("main");
    if (!main) return;
    if (!main.hasAttribute("tabindex")) main.setAttribute("tabindex", "-1");
    main.focus();
  });
  render();
}

start();
