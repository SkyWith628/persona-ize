// 앱 셸: 왼쪽 메뉴, 위쪽의 오늘 사용량과 계정 메뉴, 가운데 화면 자리(outlet).

import { usageView } from "../lib/model.js";
import { buildHash } from "../lib/router.js";
import { logout, refreshUsage } from "../session.js";
import { store } from "../store.js";
import { h, setChildren } from "../ui/dom.js";

const NAV = [
  { name: "compose", label: "첨삭" },
  { name: "contacts", label: "상대" },
  { name: "import", label: "대화 가져오기" },
  { name: "history", label: "이력" },
  { name: "data", label: "내 데이터" },
  { name: "settings", label: "설정" },
];

export function mountShell(root) {
  const links = new Map();
  const nav = h(
    "nav",
    { class: "side-nav", "aria-label": "주 메뉴" },
    h(
      "ul",
      null,
      NAV.map((item) => {
        const a = h("a", { href: buildHash(item.name), class: "side-link" }, item.label);
        links.set(item.name, a);
        return h("li", null, a);
      }),
    ),
    store.me?.is_admin
      ? h(
          "ul",
          { class: "side-extra" },
          h("li", null, h("a", { href: "/admin", class: "side-link" }, "관리자 화면", h("span", { "aria-hidden": "true" }, " ↗"))),
        )
      : null,
  );

  // ---- 오늘 사용량 ----
  const usageEl = h("a", { class: "usage-pill", href: buildHash("settings"), title: "오늘 사용량 — 눌러서 자세히 보기" });
  function renderUsage() {
    const u = usageView(store.usage);
    if (!u) {
      setChildren(usageEl, h("span", { class: "muted" }, "오늘 사용량 …"));
      return;
    }
    usageEl.classList.toggle("is-exhausted", u.exhausted);
    setChildren(
      usageEl,
      h("span", { class: "usage-label" }, "오늘 "),
      h("strong", null, `${u.rewrites} / ${u.rewriteLimit}회`),
      u.exhausted ? h("span", { class: "tag tag-warn" }, "한도 도달") : null,
    );
  }
  const offUsage = store.on("usage", renderUsage);
  renderUsage();
  refreshUsage();

  // ---- 계정 메뉴 ----
  const menuId = "account-menu";
  const menuBtn = h(
    "button",
    {
      type: "button",
      class: "btn btn-quiet account-btn",
      "aria-haspopup": "true",
      "aria-expanded": "false",
      "aria-controls": menuId,
      "aria-label": `계정 메뉴 (${store.me?.username || ""})`,
    },
    h("span", { class: "avatar", "aria-hidden": "true" }, (store.me?.username || "?").slice(0, 1).toUpperCase()),
    h("span", { class: "account-name" }, store.me?.username || ""),
    h("span", { "aria-hidden": "true" }, " ▾"),
  );
  const menu = h(
    "ul",
    { class: "menu", id: menuId, hidden: true },
    h("li", null, h("a", { href: buildHash("settings"), class: "menu-item" }, "설정")),
    h("li", null, h("a", { href: buildHash("data"), class: "menu-item" }, "내 데이터")),
    h(
      "li",
      null,
      h(
        "button",
        {
          type: "button",
          class: "menu-item",
          onClick: () => {
            closeMenu();
            logout();
          },
        },
        "로그아웃",
      ),
    ),
  );
  const account = h("div", { class: "account" }, menuBtn, menu);

  function closeMenu() {
    if (menu.hidden) return;
    menu.hidden = true;
    menuBtn.setAttribute("aria-expanded", "false");
  }
  function openMenu() {
    menu.hidden = false;
    menuBtn.setAttribute("aria-expanded", "true");
  }
  menuBtn.addEventListener("click", () => (menu.hidden ? openMenu() : closeMenu()));
  menu.addEventListener("click", (e) => {
    if (e.target.closest("a")) closeMenu();
  });
  account.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) {
      e.stopPropagation();
      closeMenu();
      menuBtn.focus();
    } else if (e.key === "ArrowDown" && e.target === menuBtn) {
      e.preventDefault();
      openMenu();
      menu.querySelector(".menu-item")?.focus();
    }
  });
  const onDocPointer = (e) => {
    if (!account.contains(e.target)) closeMenu();
  };
  const onFocusOut = (e) => {
    if (e.relatedTarget && !account.contains(e.relatedTarget)) closeMenu();
  };
  document.addEventListener("pointerdown", onDocPointer);
  account.addEventListener("focusout", onFocusOut);

  const outlet = h("main", { class: "main", id: "main", tabIndex: -1 });

  root.replaceChildren(
    h(
      "div",
      { class: "shell" },
      h(
        "aside",
        { class: "side" },
        h("a", { class: "brand", href: buildHash("compose") }, "Persona_ize"),
        nav,
        h(
          "div",
          { class: "side-foot" },
          h("p", null, "복사까지만 해요.", h("br"), "전송은 직접 합니다."),
          h("p", null, h("a", { href: "/privacy" }, "개인정보 처리방침"), " · ", h("a", { href: "/terms" }, "이용약관")),
        ),
      ),
      h("div", { class: "shell-body" }, h("header", { class: "topbar" }, h("div", { class: "topbar-right" }, usageEl, account)), outlet),
    ),
  );

  return {
    outlet,
    setActive(name) {
      for (const [key, a] of links) {
        if (key === name) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      }
      closeMenu();
    },
    destroy() {
      offUsage();
      document.removeEventListener("pointerdown", onDocPointer);
    },
  };
}
