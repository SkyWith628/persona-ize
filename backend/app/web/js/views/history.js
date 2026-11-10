// 이력(#/history): 받은 첨삭 결과 전체. 상대로 거르고, 더 보기(before)로 넘기고, 한 건·상대별로 지운다.
// 서버는 초안을 따로 저장하지 않는다 — 이력에는 결과만 있다(초안을 그대로 둔 경우에는 결과가 곧 초안).

import { api } from "../api.js";
import { describeError, isUnavailable } from "../lib/errors.js";
import { clip, formatDateTime, formatNumber } from "../lib/format.js";
import { feedbackView, mergeHistory, statusView } from "../lib/model.js";
import { buildHash } from "../lib/router.js";
import { loadContacts } from "../session.js";
import { store } from "../store.js";
import { copyText } from "../ui/clipboard.js";
import { confirmDialog } from "../ui/dialog.js";
import { busy, h, setChildren, uid } from "../ui/dom.js";
import { UNAVAILABLE_TOAST, emptyState, errorState, note, pageHead, skeleton, tag } from "../ui/parts.js";
import { extrasNode, fallbackNode, keptNode, sentenceNode, warningsNode } from "../ui/result-card.js";
import { toast } from "../ui/toast.js";

const PAGE = 30;

export function mountHistory(root, ctx) {
  let destroyed = false;
  let rows = [];
  let hasMore = false;
  let openId = null;
  let contactId = ctx.route.query.contact || "";
  let token = 0;

  const filterId = uid("filter");
  const filterEl = h("select", { id: filterId, class: "input" });
  const clearBtn = h("button", { type: "button", class: "btn btn-danger", hidden: true }, "이 상대의 이력 모두 지우기");
  const listBox = h("div", { class: "history" });
  const moreBox = h("div", { class: "history-more" });

  const aliasOf = (id) => (store.contacts || []).find((c) => c.id === id)?.alias || "지운 상대";

  function renderFilter() {
    setChildren(
      filterEl,
      h("option", { value: "" }, "모든 상대"),
      (store.contacts || []).map((c) => h("option", { value: c.id }, c.alias)),
    );
    filterEl.value = contactId;
    if (filterEl.value !== contactId) {
      contactId = "";
      filterEl.value = "";
    }
    clearBtn.hidden = !contactId;
  }

  function detailNode(rec) {
    const fb = rec.feedback;
    const recommended = rec.result.status === "recommended";
    return h(
      "div",
      { class: "history-detail" },
      recommended ? sentenceNode(rec.result) : fallbackNode(rec.result, { history: true }),
      recommended ? keptNode(rec.result) : h("p", { class: "result-text result-text-plain" }, rec.result.result_text),
      recommended ? warningsNode(rec.result) : null,
      fb && fb.edited_text
        ? h("div", { class: "extras-block" }, h("h3", { class: "extras-title" }, "내가 고쳐서 쓴 문장"), h("p", { class: "result-text result-text-plain" }, fb.edited_text))
        : null,
      h(
        "div",
        { class: "result-actions" },
        h(
          "button",
          {
            type: "button",
            class: "btn",
            onClick: async () => {
              const ok = await copyText(fb?.edited_text || rec.result.result_text);
              toast(ok ? "복사했어요. 전송은 직접 하세요." : "복사하지 못했어요. 문장을 직접 선택해 복사해 주세요.", { kind: ok ? "ok" : "warn" });
            },
          },
          "문장 복사",
        ),
        h(
          "button",
          {
            type: "button",
            class: "btn btn-danger",
            onClick: (e) => removeOne(rec, e.currentTarget),
          },
          "이 기록 지우기",
        ),
      ),
      extrasNode(rec, null),
    );
  }

  function rowNode(rec) {
    const status = statusView(rec.result);
    const fb = feedbackView(rec.feedback);
    const isOpen = openId === rec.id;
    const panelId = `hist-${rec.id}`;
    return h(
      "li",
      { class: ["history-row", isOpen ? "is-open" : ""], dataset: { id: rec.id } },
      h(
        "div",
        { class: "history-summary" },
        h(
          "div",
          { class: "history-meta" },
          h("time", { datetime: rec.created_at }, formatDateTime(rec.created_at)),
          h("span", { class: "history-who" }, aliasOf(rec.contact_id)),
          tag(`조건 ${rec.condition || "?"}`),
          tag(status.label, status.tone),
          rec.result.attempts > 1 ? tag(`${rec.result.attempts}회 시도`) : null, // 검증에 걸려 다시 만든 흔적
          fb.kind === "unknown" || fb.kind === "none" ? null : tag(fb.label, fb.tone),
        ),
        h("p", { class: "history-text" }, rec.result.result_text),
        h(
          "button",
          {
            type: "button",
            class: "btn",
            // 줄마다 같은 글자라, 읽는 이름에는 어느 결과인지(문장 앞부분·받은 때·조건) 붙인다
            "aria-label": `${isOpen ? "접기" : "자세히"}: ${clip(rec.result.result_text, 30)} (${formatDateTime(rec.created_at)}, 조건 ${rec.condition || "?"})`,
            "aria-expanded": String(isOpen),
            "aria-controls": panelId,
            onClick: () => {
              openId = isOpen ? null : rec.id;
              renderList();
              listBox.querySelector(`[data-id="${CSS.escape(rec.id)}"] .history-summary button`)?.focus();
            },
          },
          isOpen ? "접기" : "자세히",
        ),
      ),
      h("div", { class: "history-panel", id: panelId, hidden: !isOpen }, isOpen ? detailNode(rec) : null),
    );
  }

  function renderList() {
    if (!rows.length) {
      setChildren(
        listBox,
        emptyState(
          contactId ? "이 상대에게 받은 첨삭이 아직 없어요" : "아직 받은 첨삭이 없어요",
          "첨삭 화면에서 초안을 쓰고 '말투 맞추기'를 누르면 여기에 쌓여요.",
          h("a", { class: "btn", href: buildHash("compose", {}, contactId ? { contact: contactId } : {}) }, "첨삭하러 가기"),
        ),
      );
      setChildren(moreBox, null);
      return;
    }
    setChildren(listBox, h("ul", { class: "history-list" }, rows.map(rowNode)));
    setChildren(
      moreBox,
      hasMore
        ? h("button", { type: "button", class: "btn", onClick: (e) => loadMore(e.currentTarget) }, "더 보기")
        : h("p", { class: "muted" }, `여기까지예요 · ${formatNumber(rows.length)}건`),
    );
  }

  async function load() {
    token += 1;
    const mine = token;
    rows = [];
    hasMore = false;
    openId = null;
    setChildren(listBox, skeleton(4));
    setChildren(moreBox, null);
    try {
      const [, list] = await Promise.all([loadContacts(), api.rewrites.list({ contactId: contactId || undefined, limit: PAGE })]);
      if (destroyed || mine !== token) return;
      rows = Array.isArray(list) ? list : [];
      hasMore = rows.length >= PAGE;
      renderFilter();
      renderList();
    } catch (err) {
      if (destroyed || mine !== token || err.status === 401) return;
      setChildren(listBox, errorState(describeError(err), load));
    }
  }

  async function loadMore(button) {
    const last = rows[rows.length - 1];
    if (!last) return;
    await busy(button, "불러오는 중…", async () => {
      try {
        const list = await api.rewrites.list({ contactId: contactId || undefined, limit: PAGE, before: last.created_at });
        if (destroyed) return;
        const firstNew = rows.length;
        const merged = mergeHistory(rows, list);
        rows = merged.rows;
        // 새로 붙은 게 없으면 끝이다(before를 아직 모르는 서버는 같은 목록을 다시 준다)
        hasMore = merged.added > 0 && (list || []).length >= PAGE;
        renderList();
        // 목록을 다시 그리면 누른 버튼이 사라진다 — 새로 붙은 첫 줄(없으면 마지막 줄)로 포커스를 옮겨 읽던 자리를 잇는다
        const toggles = listBox.querySelectorAll(".history-summary button");
        (toggles[Math.min(firstNew, toggles.length - 1)] || root.querySelector("h1"))?.focus();
      } catch (err) {
        if (err.status !== 401) toast(describeError(err, "더 불러오지 못했어요."), { kind: "error" });
      }
    });
  }

  async function removeOne(rec, button) {
    const ok = await confirmDialog({
      title: "이 기록을 지울까요?",
      message: "이 첨삭 결과와 남긴 피드백이 지워져요. 되돌릴 수 없어요.",
      confirmLabel: "기록 지우기",
      danger: true,
    });
    if (!ok) return;
    await busy(button, "지우는 중…", async () => {
      try {
        await api.rewrites.remove(rec.id);
        const at = rows.findIndex((r) => r.id === rec.id);
        rows = rows.filter((r) => r.id !== rec.id);
        if (openId === rec.id) openId = null;
        toast("기록을 지웠어요.", { kind: "ok" });
        renderList();
        // 지운 줄의 버튼이 사라지므로 그 자리의 다음 줄(없으면 화면 제목)로 포커스를 옮긴다
        const next = rows[Math.min(at, rows.length - 1)];
        const target = next ? listBox.querySelector(`[data-id="${CSS.escape(next.id)}"] .history-summary button`) : root.querySelector("h1");
        target?.focus();
      } catch (err) {
        if (err.status === 401) return;
        toast(isUnavailable(err) ? UNAVAILABLE_TOAST : describeError(err, "지우지 못했어요."), { kind: isUnavailable(err) ? "warn" : "error" });
      }
    });
  }

  clearBtn.addEventListener("click", async () => {
    if (!contactId) return;
    const name = aliasOf(contactId);
    const ok = await confirmDialog({
      title: `'${name}'에게 받은 첨삭 이력을 모두 지울까요?`,
      message: "이 상대의 첨삭 결과와 피드백이 전부 지워져요. 대화와 말투 설정은 그대로 있어요. 되돌릴 수 없어요.",
      confirmLabel: "이력 모두 지우기",
      danger: true,
    });
    if (!ok) return;
    await busy(clearBtn, "지우는 중…", async () => {
      try {
        const d = await api.rewrites.removeByContact(contactId);
        toast(`이력 ${formatNumber(d?.deleted ?? 0)}건을 지웠어요.`, { kind: "ok" });
        load();
      } catch (err) {
        if (err.status === 401) return;
        toast(isUnavailable(err) ? UNAVAILABLE_TOAST : describeError(err, "지우지 못했어요."), { kind: isUnavailable(err) ? "warn" : "error" });
      }
    });
  });

  filterEl.addEventListener("change", () => {
    contactId = filterEl.value;
    clearBtn.hidden = !contactId;
    load();
  });

  root.replaceChildren(
    pageHead("이력", "지금까지 받은 첨삭 결과예요."),
    note("이력에는 결과 문장만 남아요. 초안은 따로 저장하지 않아요. 다만 추천하지 않고 초안을 그대로 둔 경우에는 결과가 곧 초안이에요."),
    h("div", { class: "toolbar" }, h("div", { class: "field field-inline" }, h("label", { for: filterId }, "상대"), filterEl), clearBtn),
    listBox,
    moreBox,
  );
  renderFilter();
  load();

  return () => {
    destroyed = true;
  };
}
