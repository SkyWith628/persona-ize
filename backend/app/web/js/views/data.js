// 내 데이터(#/data): 무엇이 저장돼 있는지 보고, 내려받고, 지운다.
// 상대별 요약 표 · 대화방 삭제 · 내 데이터 내려받기 · 보관 기간 안내 · 계정 삭제.

import { api } from "../api.js";
import { describeError, isMissingEndpoint } from "../lib/errors.js";
import { formatDateTime, formatNumber } from "../lib/format.js";
import { relLabel } from "../lib/model.js";
import { buildHash } from "../lib/router.js";
import { invalidateContacts, leave, loadContacts } from "../session.js";
import { forgetUserPrefs } from "../store.js";
import { openDialog } from "../ui/dialog.js";
import { busy, h, uid } from "../ui/dom.js";
import { UNAVAILABLE_TOAST, emptyState, loadInto, note, pageHead } from "../ui/parts.js";
import { toast } from "../ui/toast.js";
import { deleteConversation } from "./conversations.js";

function summaryTable(summary) {
  const rows = Array.isArray(summary?.contacts) ? summary.contacts : [];
  const totals = summary?.totals || {};
  const graph = summary?.graph_enabled !== false;
  if (!rows.length) return emptyState("아직 저장된 상대가 없어요", "상대를 추가하고 첨삭을 받으면 여기에 쌓여요.");
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "table" },
      h("caption", { class: "sr-only" }, "상대별로 저장된 데이터"),
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          h("th", { scope: "col" }, "상대"),
          h("th", { scope: "col" }, "관계"),
          h("th", { scope: "col", class: "num" }, "대화방"),
          h("th", { scope: "col", class: "num" }, "메시지"),
          h("th", { scope: "col", class: "num" }, "사건 정리"),
          h("th", { scope: "col", class: "num" }, "첨삭"),
          h("th", { scope: "col" }, "마지막 첨삭"),
        ),
      ),
      h(
        "tbody",
        null,
        rows.map((r) =>
          h(
            "tr",
            null,
            h("th", { scope: "row" }, h("a", { href: buildHash("contact", { id: r.contact_id }) }, r.alias)),
            h("td", null, relLabel(r.relationship_type)),
            h("td", { class: "num" }, formatNumber(r.conversations)),
            h("td", { class: "num" }, `${formatNumber(r.messages)} (내 메시지 ${formatNumber(r.my_messages)})`),
            h("td", { class: "num" }, graph ? `${formatNumber(r.graph_ingested)} / ${formatNumber(r.messages)}` : "쓰지 않음"),
            h(
              "td",
              { class: "num" },
              h(
                "a",
                { href: buildHash("history", {}, { contact: r.contact_id }), "aria-label": `${r.alias} 첨삭 이력 ${formatNumber(r.rewrites)}건` },
                `${formatNumber(r.rewrites)}건`,
              ),
            ),
            h("td", null, r.last_rewrite_at ? formatDateTime(r.last_rewrite_at) : "—"),
          ),
        ),
      ),
      h(
        "tfoot",
        null,
        h(
          "tr",
          null,
          h("th", { scope: "row" }, `모두 ${formatNumber(totals.contacts ?? rows.length)}명`),
          h("td", null, ""),
          h("td", { class: "num" }, formatNumber(totals.conversations)),
          h("td", { class: "num" }, formatNumber(totals.messages)),
          h("td", { class: "num" }, ""),
          h("td", { class: "num" }, `${formatNumber(totals.rewrites)}건`),
          h("td", null, ""),
        ),
      ),
    ),
  );
}

function conversationsSection() {
  const box = h("div");
  const titleEl = h("h2", { class: "section-title", tabIndex: -1 }, "가져온 대화");
  const reload = loadInto(box, {
    lines: 3,
    load: async () => {
      const [contacts, conversations] = await Promise.all([loadContacts(), api.conversations.list()]);
      return { contacts, conversations };
    },
    isEmpty: (d) => !d.conversations.length,
    empty: () => emptyState("가져온 대화가 없어요", null, h("a", { class: "btn", href: buildHash("import") }, "대화 가져오기")),
    render: ({ contacts, conversations }) => {
      const alias = new Map(contacts.map((c) => [c.id, c.alias]));
      return h(
        "ul",
        { class: "conv-list" },
        conversations.map((conv) =>
          h(
            "li",
            { class: "conv" },
            h(
              "div",
              { class: "conv-head" },
              h(
                "div",
                { class: "conv-title" },
                h("strong", null, conv.title),
                h("span", { class: "muted" }, ` · ${alias.get(conv.contact_id) || "지운 상대"} · ${formatDateTime(conv.created_at)}에 가져옴`),
              ),
              h(
                "div",
                { class: "conv-actions" },
                // 대화방마다 같은 글자라, 읽는 이름에는 어느 대화방인지 붙인다
                alias.has(conv.contact_id)
                  ? h("a", { class: "btn", href: buildHash("contact", { id: conv.contact_id }), "aria-label": `${conv.title} 대화 보기` }, "대화 보기")
                  : null,
                h(
                  "button",
                  {
                    type: "button",
                    class: "btn btn-danger",
                    "aria-label": `${conv.title} 대화방 삭제`,
                    onClick: async () => {
                      if (await deleteConversation(conv)) {
                        reload();
                        titleEl.focus(); // 지운 줄의 버튼이 사라지므로 구획 제목으로
                      }
                    },
                  },
                  "삭제",
                ),
              ),
            ),
          ),
        ),
      );
    },
  });
  return h(
    "section",
    { class: "card section" },
    titleEl,
    h("p", { class: "section-lead" }, "대화방을 지우면 메시지 원문과 그 대화로 만든 검색 자료, 지난 약속·사건 기록이 함께 지워져요."),
    box,
  );
}

async function download(button) {
  await busy(button, "준비하는 중…", async () => {
    try {
      const res = await api.exportData();
      const blob = await res.blob();
      const header = res.headers.get("Content-Disposition") || "";
      const named = /filename="?([^";]+)"?/i.exec(header);
      const today = new Date();
      const stamp = `${today.getFullYear()}${String(today.getMonth() + 1).padStart(2, "0")}${String(today.getDate()).padStart(2, "0")}`;
      const url = URL.createObjectURL(blob);
      const a = h("a", { href: url, download: named ? named[1] : `persona-ize-export-${stamp}.json`, hidden: true });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast("내 데이터 파일을 내려받았어요. 대화 원문이 들어 있으니 보관에 주의해 주세요.", { kind: "ok" });
    } catch (err) {
      if (err.status === 401) return;
      toast(isMissingEndpoint(err) ? UNAVAILABLE_TOAST : describeError(err, "내려받지 못했어요."), { kind: isMissingEndpoint(err) ? "warn" : "error" });
    }
  });
}

function deleteAccountDialog() {
  const PHRASE = "계정 삭제";
  return openDialog({
    title: "계정을 지울까요?",
    danger: true,
    render: ({ close }) => {
      const passId = uid("pw");
      const phraseId = uid("phrase");
      const errorEl = h("p", { class: "form-error", role: "alert", hidden: true });
      const passEl = h("input", { id: passId, class: "input", type: "password", autocomplete: "current-password", maxlength: 128, "data-autofocus": true });
      const phraseEl = h("input", { id: phraseId, class: "input", type: "text", autocomplete: "off", spellcheck: "false" });
      const submit = h("button", { type: "submit", class: "btn btn-danger-solid", disabled: true }, "계정과 모든 데이터 지우기");
      const sync = () => {
        submit.disabled = !(passEl.value && phraseEl.value.trim() === PHRASE);
      };
      passEl.addEventListener("input", sync);
      phraseEl.addEventListener("input", sync);
      const onSubmit = async (e) => {
        e.preventDefault();
        if (submit.disabled) return;
        errorEl.hidden = true;
        await busy(submit, "지우는 중…", async () => {
          try {
            close(await api.deleteAccount(passEl.value));
          } catch (err) {
            errorEl.textContent =
              err.status === 401 ? "비밀번호가 맞지 않아요." : describeError(err, "지우지 못했어요. 잠시 뒤 다시 시도해 주세요.");
            errorEl.hidden = false;
            passEl.focus();
            passEl.select();
          }
        });
        sync();
      };
      return h(
        "form",
        { class: "dialog-form", novalidate: true, onSubmit },
        h("p", { class: "dialog-message" }, "계정과 함께 아래가 모두 지워져요. 되돌릴 수 없어요."),
        h(
          "ul",
          { class: "dialog-bullets" },
          h("li", null, "모든 상대와 말투 설정"),
          h("li", null, "가져온 대화 원문, 검색 자료, 지난 약속·사건 기록"),
          h("li", null, "첨삭 이력과 피드백, 사용량 기록"),
          h("li", null, "로그인 정보(모든 기기에서 로그아웃)"),
        ),
        h("p", { class: "hint" }, "필요하면 먼저 '내 데이터 내려받기'로 사본을 받아 두세요."),
        h("div", { class: "field" }, h("label", { for: passId }, "비밀번호"), passEl),
        h("div", { class: "field" }, h("label", { for: phraseId }, `확인하려면 "${PHRASE}"라고 입력해 주세요`), phraseEl),
        errorEl,
        h("div", { class: "dialog-actions" }, h("button", { type: "button", class: "btn", onClick: () => close(undefined) }, "취소"), submit),
      );
    },
  });
}

export function mountData(root) {
  const summaryBox = h("div");
  // 보관 기간은 서버 설정이라 요약을 읽어야 알 수 있다 — 읽기 전에는 단정하지 않는다
  const retentionEl = h("li", null, "첨삭 이력을 얼마 동안 보관하는지는 서버 설정에 따라 달라요. 지금은 확인할 수 없어요.");

  loadInto(summaryBox, {
    lines: 4,
    load: () => api.dataSummary(),
    missingIs404: true,
    unavailableTitle: "상대별 요약은 지금 볼 수 없어요",
    render: (summary) => {
      const days = Number(summary?.retention_days) || 0;
      retentionEl.textContent = days > 0 ? `첨삭 이력은 ${formatNumber(days)}일이 지나면 자동으로 지워져요.` : "첨삭 이력은 직접 지울 때까지 보관돼요.";
      return summaryTable(summary);
    },
  });

  const removeAccount = async () => {
    const result = await deleteAccountDialog();
    if (!result) return;
    forgetUserPrefs();
    invalidateContacts();
    const failed = result.graph_error ? " 지난 약속·사건 기록 일부는 지우지 못했을 수 있어요." : "";
    leave({
      text: `계정을 지웠어요 — 상대 ${formatNumber(result.contacts)}명, 대화방 ${formatNumber(result.conversations)}개, 메시지 ${formatNumber(result.messages)}개.${failed}`,
      tone: result.graph_error ? "warn" : "ok",
    });
  };

  root.replaceChildren(
    pageHead("내 데이터", "내 계정에 무엇이 저장돼 있는지 보고, 내려받거나 지울 수 있어요."),
    h(
      "section",
      { class: "card section" },
      h("h2", { class: "section-title" }, "상대별 요약"),
      h("p", { class: "section-lead" }, "'사건 정리'는 지난 약속·사건으로 정리를 마친 메시지 수예요."),
      summaryBox,
    ),
    conversationsSection(),
    h(
      "section",
      { class: "card section" },
      h("h2", { class: "section-title" }, "내 데이터 내려받기"),
      h("p", { class: "section-lead" }, "상대, 말투 설정의 모든 버전, 가져온 대화, 첨삭 이력을 파일 하나(JSON)로 받아요."),
      h("button", { type: "button", class: "btn", onClick: (e) => download(e.currentTarget) }, "내 데이터 내려받기"),
    ),
    h(
      "section",
      { class: "card section" },
      h("h2", { class: "section-title" }, "보관 기간"),
      h(
        "ul",
        { class: "plain-list" },
        h("li", null, "가져온 대화와 말투 설정은 직접 지울 때까지 보관돼요. 대화방·상대·계정 단위로 지울 수 있어요."),
        retentionEl,
        h("li", null, "초안은 따로 저장하지 않아요. 다만 추천하지 않고 초안을 그대로 둔 경우에는 그 결과(초안과 같은 문장)가 이력에 남아요."),
        h("li", null, "지우면 원문과 그걸로 만든 검색 자료·사건 기록이 함께 지워져요."),
      ),
      note(["자세한 내용은 ", h("a", { href: "/privacy" }, "개인정보 처리방침"), "에 있어요."]),
    ),
    h(
      "section",
      { class: "card section section-danger" },
      h("h2", { class: "section-title" }, "계정 삭제"),
      h("p", { class: "section-lead" }, "계정과 모든 데이터를 지워요. 되돌릴 수 없어요."),
      h("button", { type: "button", class: "btn btn-danger", onClick: removeAccount }, "계정 지우기"),
    ),
  );
  return () => {};
}
