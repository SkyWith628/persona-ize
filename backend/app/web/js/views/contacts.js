// 상대 목록(#/contacts): 이름, 관계, 말투 설정 버전, 가져온 대화 수. 추가는 이름+관계만.

import { api } from "../api.js";
import { formatNumber, initial, withJosa } from "../lib/format.js";
import { relLabel } from "../lib/model.js";
import { buildHash } from "../lib/router.js";
import { loadContacts } from "../session.js";
import { addContactDialog } from "../ui/contact-dialog.js";
import { h } from "../ui/dom.js";
import { emptyState, loadInto, note, pageHead, tag } from "../ui/parts.js";
import { toast } from "../ui/toast.js";

export function mountContacts(root, ctx) {
  const listBox = h("div");

  async function add() {
    const contact = await addContactDialog();
    if (!contact) return;
    if (contact.persona_version == null) {
      toast(`${withJosa(contact.alias, "을/를")} 추가했어요. 말투 설정을 확인하고 저장해 주세요.`, { kind: "info" });
    } else {
      toast(`${withJosa(contact.alias, "을/를")} 추가했어요. 관계에 맞는 기본 말투로 시작해요.`, { kind: "ok" });
    }
    ctx.navigate(buildHash("contact", { id: contact.id }));
  }

  function render({ contacts, summary }) {
    const byId = new Map((summary?.contacts || []).map((row) => [row.contact_id, row]));
    return [
      h(
        "div",
        { class: "table-wrap card" },
        h(
          "table",
          { class: "table" },
          h("caption", { class: "sr-only" }, "상대 목록"),
          h(
            "thead",
            null,
            h(
              "tr",
              null,
              h("th", { scope: "col" }, "이름"),
              h("th", { scope: "col" }, "관계"),
              h("th", { scope: "col" }, "말투 설정"),
              h("th", { scope: "col" }, "가져온 대화"),
              h("th", { scope: "col" }, h("span", { class: "sr-only" }, "바로 가기")),
            ),
          ),
          h(
            "tbody",
            null,
            contacts.map((c) => {
              const row = byId.get(c.id);
              return h(
                "tr",
                null,
                h(
                  "th",
                  { scope: "row" },
                  h(
                    "a",
                    { class: "row-link", href: buildHash("contact", { id: c.id }) },
                    h("span", { class: "avatar", "aria-hidden": "true" }, initial(c.alias)),
                    c.alias,
                  ),
                ),
                h("td", null, relLabel(c.relationship_type)),
                h("td", null, c.persona_version == null ? tag("설정 없음", "warn") : tag(`v${c.persona_version}`)),
                h(
                  "td",
                  null,
                  row ? `대화방 ${formatNumber(row.conversations)}개 · 메시지 ${formatNumber(row.messages)}개` : h("span", { class: "muted" }, "—"),
                ),
                h(
                  "td",
                  { class: "cell-actions" },
                  // 줄마다 같은 글자라, 읽는 이름에는 누구의 것인지 붙인다
                  h("a", { class: "btn", href: buildHash("compose", {}, { contact: c.id }), "aria-label": `${c.alias} 첨삭하기` }, "첨삭하기"),
                ),
              );
            }),
          ),
        ),
      ),
      summary ? null : note("가져온 대화 수는 지금 볼 수 없어요. 상대를 누르면 대화방을 볼 수 있어요."),
    ];
  }

  root.replaceChildren(
    pageHead(
      "상대",
      "메시지를 보낼 사람이에요. 상대마다 말투 설정을 따로 둬요.",
      h("button", { type: "button", class: "btn btn-primary", onClick: add }, "상대 추가"),
    ),
    listBox,
  );

  loadInto(listBox, {
    lines: 4,
    load: async () => {
      const [contacts, summary] = await Promise.all([
        loadContacts({ force: true }),
        api.dataSummary().catch(() => null), // 대화 수는 덤 — 못 읽어도 목록은 보여 준다
      ]);
      return { contacts, summary };
    },
    isEmpty: (data) => !data.contacts.length,
    empty: () =>
      emptyState(
        "아직 상대가 없어요",
        "메시지를 보낼 사람을 추가해 주세요. 이름과 관계만 고르면 관계에 맞는 기본 말투로 시작해요.",
        h("button", { type: "button", class: "btn", onClick: add }, "첫 상대 추가"),
      ),
    render,
  });

  if (ctx.route.query.new) add();
  return () => {};
}
