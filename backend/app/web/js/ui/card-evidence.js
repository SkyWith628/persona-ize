// 말투 제안 카드의 근거 메시지 — 버튼을 누르면 카드 안에서 펼쳐 원문을 보여 준다.
// 새 API는 없다: 카드에 실려 오는 근거 id(`<대화방 키>:<메시지 키>`)를 상대의 대화방 목록과 메시지 조회로
// 풀어 지금 남아 있는 원문만 보인다(저장된 그대로 — 가져올 때 가려진 곳은 가려진 채로).

import { api } from "../api.js";
import { formatDateTime } from "../lib/format.js";
import { evidenceCountText, evidenceGroups, evidencePlan, evidenceRooms } from "../lib/persona.js";
import { h, uid } from "./dom.js";
import { loadInto, note } from "./parts.js";

const ROLE_LABEL = { me: "나", contact: "상대" };
const NOT_FOUND = "근거 메시지를 찾을 수 없어요. 대화를 지웠거나 화자를 고친 뒤일 수 있어요.";

async function loadEvidence(plan, contactId) {
  const rooms = evidenceRooms(plan, await api.conversations.list(contactId));
  const loaded = await Promise.all(
    rooms.map(async (conversation) => ({ conversation, messages: await api.conversations.messages(conversation.id) })),
  );
  return evidenceGroups(plan, loaded);
}

function renderEvidence(found) {
  if (!found.groups.length) return note(NOT_FOUND);
  return [
    found.groups.map((group) =>
      h(
        "section",
        { class: ["card-evidence-group", `is-${group.key}`] },
        h("p", { class: "card-evidence-label" }, group.label, " ", h("span", { class: "muted" }, `(${evidenceCountText(group)})`)),
        h(
          "ul",
          { class: "card-evidence-messages" },
          group.messages.map((m) =>
            h(
              "li",
              null,
              h(
                "span",
                { class: "card-evidence-meta" },
                h("span", { class: "card-evidence-who" }, ROLE_LABEL[m.speaker_role] || "상대"),
                " ",
                h("time", { datetime: m.sent_at }, formatDateTime(m.sent_at)),
              ),
              h("p", { class: "card-evidence-text" }, m.text),
            ),
          ),
        ),
      ),
    ),
    found.missing ? note(`근거 메시지 ${found.missing}건은 지금 남아 있지 않아 빼고 보여 드려요.`) : null,
  ];
}

/**
 * 제안 카드에 붙이는 "근거 메시지 보기" 버튼과 펼침 영역. 고를 근거가 없으면 null.
 * 펼칠 때 처음 한 번만 읽어 온다(실패하면 다시 시도 버튼이 나온다).
 */
export function cardEvidence(card, contactId) {
  const plan = evidencePlan(card);
  if (!plan || !contactId) return null;
  const panelId = uid("card-evidence");
  const panel = h("div", { class: "card-evidence-panel", id: panelId, role: "region", "aria-label": "근거 메시지", hidden: true });
  const label = h("span", { class: "card-evidence-toggle-label" }, "근거 메시지 보기");
  const caret = h("span", { class: "card-evidence-caret", "aria-hidden": "true" }, "▸");
  let loaded = false;
  const button = h(
    "button",
    {
      type: "button",
      class: "btn btn-quiet card-evidence-toggle",
      "aria-expanded": "false",
      "aria-controls": panelId,
      onClick: () => {
        const open = button.getAttribute("aria-expanded") !== "true";
        button.setAttribute("aria-expanded", String(open));
        label.textContent = open ? "근거 메시지 접기" : "근거 메시지 보기";
        caret.textContent = open ? "▾" : "▸";
        panel.hidden = !open;
        if (!open || loaded) return;
        loaded = true;
        loadInto(panel, { lines: 3, load: () => loadEvidence(plan, contactId), render: renderEvidence });
      },
    },
    caret,
    label,
  );
  return h("div", { class: "card-evidence" }, button, panel);
}
