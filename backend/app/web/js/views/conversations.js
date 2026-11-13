// 과거 대화: 대화방 목록 → 메시지 보기, 대화방 삭제. 상대 상세 화면과 내 데이터 화면이 함께 쓴다.

import { api } from "../api.js";
import { describeError } from "../lib/errors.js";
import { clip, dayKey, errorCode, formatDateTime, formatDay, formatNumber, formatTime } from "../lib/format.js";
import { graphPhaseView } from "../lib/importing.js";
import { ME_NAME_MAX, asksMeName, swapErrorText, swapNotes, swapPreview, swapRequest, swapResultView } from "../lib/roles.js";
import { buildHash } from "../lib/router.js";
import { confirmDialog, openDialog } from "../ui/dialog.js";
import { h, setChildren, uid } from "../ui/dom.js";
import { emptyState, loadInto, meter, note } from "../ui/parts.js";
import { toast } from "../ui/toast.js";

const PAGE = 300; // 한 번에 그리는 메시지 수(긴 대화에서 화면이 느려지지 않게)

/**
 * 대화방 삭제: 무엇이 지워지는지 알리고 확인을 받은 뒤 지운다.
 * @returns {Promise<boolean>} 지웠으면 true
 */
export async function deleteConversation(conv) {
  const ok = await confirmDialog({
    title: `'${conv.title}' 대화방을 지울까요?`,
    message: "이 대화방에서 가져온 것이 모두 지워져요. 되돌릴 수 없어요.",
    bullets: ["메시지 원문", "말투 검색에 쓰는 자료(메시지 묶음과 검색용 값)", "이 대화방에서 정리한 지난 약속·사건 기록"],
    confirmLabel: "대화방 지우기",
    danger: true,
  });
  if (!ok) return false;
  try {
    const d = await api.conversations.remove(conv.id);
    toast(
      `대화방을 지웠어요 — 메시지 ${formatNumber(d.messages)}개, 말투 묶음 ${formatNumber(d.episodes)}개, 사건 기록 ${formatNumber(d.graph_episodes)}개.`,
      { kind: "ok" },
    );
    if (d.graph_error) {
      const why = errorCode(d.graph_error);
      toast(`지난 약속·사건 기록 일부는 지우지 못했어요${why ? `(${why})` : ""}. 원문과 검색 자료는 지워졌어요.`, { kind: "warn", timeout: 0 });
    }
    return true;
  } catch (err) {
    if (err.status !== 401) toast(describeError(err, "지우지 못했어요."), { kind: "error" });
    return false;
  }
}

/**
 * 화자를 거꾸로 지정해 가져온 대화방 고치기: 나와 상대를 맞바꾼다(대화방은 그대로 둔다).
 * 서버에는 역할만 남아 있어 화자별로 따로 고칠 수는 없다 — 방 전체를 맞바꾼다.
 * @param {object|null} stats 대화방 통계(없으면 사건 정리 안내와 이름 칸을 빼고 묻는다)
 * @returns {Promise<boolean>} 서버에 반영됐으면 true
 */
async function swapRoles(conv, contactAlias, messages, stats) {
  const preview = swapPreview(messages, contactAlias);
  const nameId = uid("me-name");
  const nameEl = asksMeName(stats)
    ? h("input", { id: nameId, class: "input", type: "text", maxlength: ME_NAME_MAX, autocomplete: "off", placeholder: "예: 김민수" })
    : null;
  const answer = await openDialog({
    title: `'${conv.title}'에서 나와 상대를 맞바꿀까요?`,
    render: ({ close }) =>
      h(
        "form",
        {
          method: "dialog",
          class: "dialog-form",
          onSubmit: (e) => {
            e.preventDefault();
            close({ meName: nameEl ? nameEl.value : "" });
          },
        },
        h(
          "p",
          { class: "dialog-message" },
          `가져올 때 화자를 거꾸로 골랐다면 여기서 고칠 수 있어요. 지금 내 메시지 ${formatNumber(preview.mineNow)}개가 ${contactAlias}의 말이 되고, ${contactAlias}의 메시지 ${formatNumber(preview.mineAfter)}개가 내 말이 돼요.`,
        ),
        preview.sample.length
          ? h(
              "ul",
              { class: "dialog-bullets", "aria-label": "바꾼 뒤 미리보기" },
              preview.sample.map((s) => h("li", null, h("strong", null, `${s.now} → ${s.after}`), ` : ${clip(s.text, 40)}`)),
            )
          : null,
        h(
          "ul",
          { class: "dialog-bullets" },
          swapNotes(stats).map((line) => h("li", null, line)),
        ),
        nameEl
          ? h(
              "div",
              { class: "field" },
              h("label", { for: nameId }, "내 이름 (선택)"),
              nameEl,
              h("p", { class: "hint" }, "지난 약속·사건 기록에 내 이름으로 적어요. 비워 두면 '사용자'로 적어요."),
            )
          : null,
        h(
          "div",
          { class: "dialog-actions" },
          h("button", { type: "button", class: "btn", "data-autofocus": true, onClick: () => close(null) }, "취소"),
          h("button", { type: "submit", class: "btn btn-primary" }, "맞바꾸기"),
        ),
      ),
  });
  if (!answer) return false;
  const body = swapRequest(messages, answer.meName);
  if (!body) return false;
  try {
    const result = await api.conversations.setSpeakerRoles(conv.id, body);
    const view = swapResultView(result);
    toast(view.text, { kind: view.kind, timeout: view.kind === "ok" ? 9000 : undefined });
    return true;
  } catch (err) {
    if (err.status !== 401) toast(swapErrorText(err) || describeError(err, "바꾸지 못했어요."), { kind: "error" });
    return false;
  }
}

function messagesNode(messages, contactAlias) {
  let shown = Math.min(messages.length, PAGE);
  const listEl = h("ol", { class: "chat", "aria-label": "메시지" });
  const moreEl = h("div", { class: "chat-more" });

  function render() {
    const slice = messages.slice(messages.length - shown);
    const rows = [];
    let lastDay = "";
    for (const m of slice) {
      const day = dayKey(m.sent_at);
      if (day !== lastDay) {
        lastDay = day;
        rows.push(h("li", { class: "chat-day" }, h("span", null, formatDay(m.sent_at))));
      }
      const mine = m.speaker_role === "me";
      rows.push(
        h(
          "li",
          { class: ["chat-row", mine ? "is-me" : "is-them"] },
          h("span", { class: "chat-who" }, mine ? "나" : contactAlias),
          h("p", { class: "chat-bubble" }, m.text),
          h("time", { class: "chat-time", datetime: m.sent_at }, formatTime(m.sent_at)),
        ),
      );
    }
    setChildren(listEl, rows);
    const hidden = messages.length - shown;
    setChildren(
      moreEl,
      hidden > 0
        ? h(
            "button",
            {
              type: "button",
              class: "btn",
              onClick: () => {
                shown = Math.min(messages.length, shown + PAGE);
                render();
              },
            },
            `앞의 메시지 ${formatNumber(Math.min(hidden, PAGE))}개 더 보기`,
          )
        : null,
    );
  }
  render();
  return [moreEl, listEl];
}

/** 대화방 하나의 내용(메시지 + 사건 정리 진행). */
function conversationBody(conv, contactAlias) {
  const statsEl = h("div", { class: "conv-stats" });
  const body = h("div", { class: "conv-body" });
  let latestStats = null; // 맞바꾸기 확인 창이 사건 정리 안내를 넣을지 정하는 데 쓴다
  const reload = loadInto(body, {
    lines: 4,
    load: () => api.conversations.messages(conv.id),
    isEmpty: (list) => !list.length,
    empty: () => emptyState("이 대화방에는 메시지가 없어요", "대화 가져오기에서 메시지를 올릴 수 있어요."),
    render: (list) => {
      const mine = list.filter((m) => m.speaker_role === "me").length;
      const swapBtn = h(
        "button",
        {
          type: "button",
          class: "btn",
          "aria-label": `${conv.title} 나와 상대 맞바꾸기`,
          onClick: async () => {
            if (swapBtn.getAttribute("aria-busy") === "true") return;
            swapBtn.setAttribute("aria-busy", "true");
            const stats = latestStats || (await api.conversations.stats(conv.id).catch(() => null));
            const changed = await swapRoles(conv, contactAlias, list, stats);
            swapBtn.removeAttribute("aria-busy");
            if (!changed || !body.isConnected) return;
            reload();
            loadStats();
          },
        },
        "나·상대 맞바꾸기",
      );
      return [
        h("p", { class: "muted" }, `메시지 ${formatNumber(list.length)}개 · 내 메시지 ${formatNumber(mine)}개`),
        h("div", { class: "conv-roles" }, h("span", { class: "muted" }, "나와 상대가 뒤바뀌어 보이나요? 대화방을 지우지 않고 고칠 수 있어요."), swapBtn),
        statsEl,
        messagesNode(list, contactAlias),
      ];
    },
  });
  // 사건 정리 진행은 서버가 통계를 줄 때만 한 줄 보여 준다(없으면 조용히 넘어간다)
  function loadStats() {
    api.conversations
      .stats(conv.id)
      .then((stats) => {
        latestStats = stats;
        if (!statsEl.isConnected && !body.isConnected) return;
        const view = graphPhaseView(stats, null);
        if (!view.status || view.status === "skipped") {
          setChildren(statsEl, null);
          return;
        }
        setChildren(
          statsEl,
          h("p", { class: "muted" }, `${view.title}${view.count ? ` · ${view.count}` : ""}`),
          view.pct != null && view.polling ? meter(stats.graph_ingested, stats.messages, "지난 약속·사건 정리") : null,
        );
      })
      .catch(() => {});
  }
  loadStats();
  return body;
}

/**
 * 상대 한 명의 대화방 목록.
 * @param {HTMLElement} box
 * @param {{contact: object}} opts
 */
export function mountConversations(box, { contact }) {
  const listBox = h("div");
  let openId = null;

  function render(list) {
    return h(
      "ul",
      { class: "conv-list" },
      list.map((conv) => {
        const isOpen = openId === conv.id;
        const panel = h("div", { class: "conv-panel", hidden: !isOpen });
        if (isOpen) panel.append(conversationBody(conv, contact.alias));
        const toggle = h(
          "button",
          {
            type: "button",
            class: "btn",
            // 대화방마다 같은 글자라, 읽는 이름에는 어느 대화방인지 붙인다
            "aria-label": `${conv.title} ${isOpen ? "대화 접기" : "대화 보기"}`,
            "aria-expanded": String(isOpen),
            onClick: () => {
              openId = isOpen ? null : conv.id;
              setChildren(listBox, render(list));
              listBox.querySelector(`[data-conv="${CSS.escape(conv.id)}"] button`)?.focus();
            },
          },
          isOpen ? "대화 접기" : "대화 보기",
        );
        return h(
          "li",
          { class: "conv", dataset: { conv: conv.id } },
          h(
            "div",
            { class: "conv-head" },
            h("div", { class: "conv-title" }, h("strong", null, conv.title), h("span", { class: "muted" }, ` · ${formatDateTime(conv.created_at)}에 가져옴`)),
            h(
              "div",
              { class: "conv-actions" },
              toggle,
              h(
                "button",
                {
                  type: "button",
                  class: "btn btn-danger",
                  "aria-label": `${conv.title} 대화방 삭제`,
                  onClick: async () => {
                    if (await deleteConversation(conv)) {
                      if (openId === conv.id) openId = null;
                      reload();
                      box.querySelector(".section-title")?.focus(); // 지운 줄의 버튼이 사라지므로 구획 제목으로
                    }
                  },
                },
                "삭제",
              ),
            ),
          ),
          panel,
        );
      }),
    );
  }

  const reload = loadInto(listBox, {
    lines: 2,
    load: () => api.conversations.list(contact.id),
    isEmpty: (list) => !list.length,
    empty: () =>
      emptyState(
        "아직 가져온 대화가 없어요",
        "없어도 말투 설정만으로 쓸 수 있어요. 대화를 가져오면 내가 실제로 쓴 표현까지 참고해요.",
        h("a", { class: "btn", href: buildHash("import", {}, { contact: contact.id }), "aria-label": `${contact.alias} 대화 가져오기` }, "대화 가져오기"),
      ),
    render,
  });

  setChildren(
    box,
    h(
      "div",
      { class: "section-head" },
      h("h2", { class: "section-title", tabIndex: -1 }, "과거 대화"),
      // 왼쪽 메뉴의 '대화 가져오기'와 달리 이 상대를 골라 둔 채로 간다 — 읽는 이름으로 구분한다
      h(
        "div",
        { class: "section-actions" },
        h("a", { class: "btn", href: buildHash("import", {}, { contact: contact.id }), "aria-label": `${contact.alias} 대화 가져오기` }, "대화 가져오기"),
      ),
    ),
    h("p", { class: "section-lead" }, "이 상대와 나눈 대화예요. 내 메시지에서 말투를 읽고, 상대 메시지는 앞뒤 맥락으로만 써요."),
    listBox,
    note("대화방을 지우면 원문과 그 대화로 만든 검색 자료·사건 기록이 함께 지워져요."),
  );
}
