// 대화 가져오기(#/import) — 네 단계: ① 상대 ② 방법 ③ 미리보기·화자 지정 ④ 가져오기(두 단계 진행).
// 파일은 브라우저에서 읽어 글자로 바꾼 뒤(UTF-8 → EUC-KR·UTF-16) JSON 본문으로 보낸다(multipart 없음).
// 저장 안내는 실제 동작 그대로: 계정에 저장되고, 검색·사건 정리에 쓰이고, 언제든 지울 수 있다.

import { api } from "../api.js";
import { isMissingEndpoint, isUnavailable } from "../lib/errors.js";
import { countCodePoints, formatBytes, formatDateTime, formatNumber, initial, josa, localOffsetMinutes } from "../lib/format.js";
import {
  METHODS,
  ROLE_OPTIONS,
  checkRoles,
  checkSize,
  decodeBytes,
  encodingLabel,
  graphPhaseView,
  keptMessages,
  meName,
  previewView,
  stylePhaseView,
} from "../lib/importing.js";
import { relLabel } from "../lib/model.js";
import { buildHash } from "../lib/router.js";
import { loadContacts } from "../session.js";
import { store } from "../store.js";
import { addContactDialog } from "../ui/contact-dialog.js";
import { busy, h, setChildren, uid } from "../ui/dom.js";
import { emptyState, errorState, meter, note, pageHead, skeleton, unavailable } from "../ui/parts.js";

const STEPS = ["상대", "방법", "미리보기", "가져오기"];
const NEW_CONVERSATION = "__new__";

export function mountImport(root, ctx) {
  let destroyed = false;
  let pollTimer = null;
  const st = {
    step: 1,
    contactId: ctx.route.query.contact || null,
    method: "kakao",
    file: null, // { name, size, encoding, chars, replaced }
    fileText: "",
    pasted: "",
    sent: null, // 미리보기에 보낸 { format, text, tz }
    view: null, // previewView 결과
    roles: {},
    keepRecent: null,
    existing: [], // 이 상대의 기존 대화방
    target: NEW_CONVERSATION,
    title: "",
    agreed: false,
  };

  const stepper = h("ol", { class: "stepper", "aria-label": "가져오기 단계" });
  const body = h("div", { class: "wizard-body" });
  const liveEl = h("p", { class: "sr-only", role: "status" });
  root.replaceChildren(
    pageHead("대화 가져오기", "상대와 나눈 대화를 올리면, 내가 그 사람에게 실제로 쓰던 표현까지 참고해서 다듬어요."),
    stepper,
    body,
    liveEl,
  );

  const contacts = () => store.contacts || [];
  const contact = () => contacts().find((c) => c.id === st.contactId) || null;

  function renderStepper() {
    setChildren(
      stepper,
      STEPS.map((label, i) => {
        const n = i + 1;
        const state = n < st.step ? "done" : n === st.step ? "current" : "todo";
        return h(
          "li",
          { class: `step step-${state}`, "aria-current": state === "current" ? "step" : null },
          h("span", { class: "step-num", "aria-hidden": "true" }, state === "done" ? "✓" : String(n)),
          h("span", { class: "step-label" }, label),
          h("span", { class: "sr-only" }, state === "done" ? " (완료)" : state === "current" ? " (지금 단계)" : ""),
        );
      }),
    );
  }

  function go(step) {
    st.step = step;
    renderStepper();
    if (step === 1) renderContactStep();
    else if (step === 2) renderMethodStep();
    else if (step === 3) renderPreviewStep();
    else renderRunStep();
    liveEl.textContent = `${step}단계: ${STEPS[step - 1]}`;
    body.querySelector("h2")?.focus();
  }

  const stepTitle = (text) => h("h2", { class: "section-title", tabIndex: -1 }, text);
  const errorLine = () => h("p", { class: "form-error", role: "alert", hidden: true });
  const showError = (el, message) => {
    el.textContent = message || "";
    el.hidden = !message;
  };

  // ---------- ① 상대 ----------
  function renderContactStep() {
    const list = contacts();
    const err = errorLine();
    const add = async () => {
      const created = await addContactDialog();
      if (!created || destroyed) return;
      st.contactId = created.id;
      renderContactStep();
    };
    if (!list.length) {
      setChildren(
        body,
        h(
          "section",
          { class: "card section" },
          stepTitle("누구와 나눈 대화인가요?"),
          emptyState(
            "아직 상대가 없어요",
            "대화를 가져올 상대를 먼저 추가해 주세요.",
            h("button", { type: "button", class: "btn btn-primary", onClick: add }, "상대 추가"),
          ),
        ),
      );
      return;
    }
    const name = uid("contact");
    setChildren(
      body,
      h(
        "form",
        {
          class: "card section",
          novalidate: true,
          onSubmit: (e) => {
            e.preventDefault();
            if (!contact()) {
              showError(err, "상대를 골라 주세요.");
              return;
            }
            go(2);
          },
        },
        stepTitle("누구와 나눈 대화인가요?"),
        h("p", { class: "section-lead" }, "상대 한 명당 대화 하나씩 가져와요. 단체방은 미리보기에서 다른 사람을 제외할 수 있어요."),
        h(
          "fieldset",
          { class: "option-list option-scroll" },
          h("legend", { class: "sr-only" }, "상대 고르기"),
          list.map((c) => {
            const inputId = uid("c");
            return h(
              "div",
              { class: "option" },
              h("input", {
                type: "radio",
                id: inputId,
                name,
                value: c.id,
                checked: c.id === st.contactId,
                onChange: () => {
                  st.contactId = c.id;
                  showError(err, "");
                },
              }),
              h(
                "label",
                { for: inputId },
                h("span", { class: "avatar", "aria-hidden": "true" }, initial(c.alias)),
                h("span", { class: "option-title" }, c.alias),
                h("span", { class: "option-desc" }, relLabel(c.relationship_type)),
              ),
            );
          }),
        ),
        err,
        h(
          "div",
          { class: "form-actions" },
          h("button", { type: "submit", class: "btn btn-primary" }, "다음"),
          h("button", { type: "button", class: "btn", onClick: add }, "새 상대 추가"),
        ),
      ),
    );
  }

  // ---------- ② 방법 ----------
  async function readFile(file, infoEl, err) {
    showError(err, "");
    setChildren(infoEl, skeleton(1, "파일을 읽는 중이에요"));
    try {
      const buffer = await file.arrayBuffer();
      const decoded = decodeBytes(new Uint8Array(buffer));
      if (destroyed) return;
      st.fileText = decoded.text;
      st.file = {
        name: file.name,
        size: file.size,
        encoding: decoded.encoding,
        chars: countCodePoints(decoded.text),
        replaced: decoded.replaced,
      };
    } catch {
      st.file = null;
      st.fileText = "";
      setChildren(infoEl, null);
      showError(err, "파일을 읽지 못했어요. 다른 파일로 다시 시도해 주세요.");
      return;
    }
    renderFileInfo(infoEl);
  }

  function renderFileInfo(infoEl) {
    const f = st.file;
    if (!f) {
      setChildren(infoEl, null);
      return;
    }
    setChildren(
      infoEl,
      h(
        "p",
        { class: "file-info" },
        h("strong", null, f.name),
        ` · ${formatBytes(f.size)} · ${formatNumber(f.chars)}자 · 글자 인코딩 ${encodingLabel(f.encoding)}`,
      ),
      f.replaced > 0
        ? h("p", { class: "note note-warn" }, `글자 ${formatNumber(f.replaced)}개가 깨져 보여요. 내보낸 파일이 맞는지 확인해 주세요.`)
        : null,
    );
  }

  function renderMethodStep() {
    const c = contact();
    if (!c) {
      go(1);
      return;
    }
    const name = uid("method");
    const err = errorLine();
    const unavailableBox = h("div");
    const inputBox = h("div", { class: "method-input" });
    const nextBtn = h("button", { type: "submit", class: "btn btn-primary" }, "미리보기");

    function renderInput() {
      const method = METHODS.find((m) => m.value === st.method);
      if (method.file) {
        const fileId = uid("file");
        const infoEl = h("div", { class: "file-box" });
        const fileEl = h("input", {
          id: fileId,
          class: "file-input",
          type: "file",
          accept: method.accept,
          onChange: () => {
            const file = fileEl.files && fileEl.files[0];
            if (file) readFile(file, infoEl, err);
          },
        });
        setChildren(
          inputBox,
          h(
            "div",
            { class: "field" },
            h("label", { for: fileId }, method.value === "kakao" ? "카카오톡에서 내보낸 파일" : "텔레그램에서 내보낸 result.json"),
            fileEl,
            h(
              "p",
              { class: "hint" },
              method.value === "kakao"
                ? "채팅방 메뉴의 '대화 내용 내보내기'로 만든 .txt 파일이에요(PC는 .csv도 돼요). 카카오톡 버전마다 파일 모양이 달라 일부 줄을 못 읽을 수 있고, 못 읽은 줄 수는 미리보기에서 알려 드려요."
                : "PC용 텔레그램(Telegram Desktop)에서 채팅 기록을 JSON 형식으로 내보내면 생기는 result.json이에요. 모바일 앱에서는 만들 수 없어요.",
            ),
            infoEl,
          ),
        );
        renderFileInfo(infoEl);
      } else {
        const pasteId = uid("paste");
        const pasteEl = h("textarea", {
          id: pasteId,
          class: "textarea mono",
          rows: 9,
          spellcheck: "false",
          placeholder: "2026-10-05 09:25 | 박지현 | 민수씨, 개편은 언제까지 가능해요?\n2026-10-05 09:31 | 김민수 | 팀장님, 다음 주 수요일까지는 가능합니다.",
          value: st.pasted,
          onInput: () => {
            st.pasted = pasteEl.value;
          },
        });
        setChildren(
          inputBox,
          h(
            "div",
            { class: "field" },
            h("label", { for: pasteId }, "대화 붙여넣기"),
            pasteEl,
            h("p", { class: "hint" }, "한 줄에 메시지 하나: ", h("code", null, "날짜 시각 | 보낸 사람 | 내용"), " 형식으로 붙여 넣어 주세요."),
          ),
        );
      }
    }

    const onSubmit = async (e) => {
      e.preventDefault();
      showError(err, "");
      setChildren(unavailableBox, null);
      const method = METHODS.find((m) => m.value === st.method);
      const text = method.file ? st.fileText : st.pasted;
      if (method.file && !st.file) {
        showError(err, "파일을 골라 주세요.");
        return;
      }
      const problem = checkSize(text);
      if (problem) {
        showError(err, problem);
        return;
      }
      const sent = { format: method.format, text, tz_offset_minutes: localOffsetMinutes() };
      await busy(nextBtn, "읽는 중…", async () => {
        try {
          const [preview, existing] = await Promise.all([
            api.imports.parse(sent),
            api.conversations.list(c.id).catch(() => []),
          ]);
          if (destroyed) return;
          st.sent = sent;
          st.view = previewView(preview, c.alias);
          st.roles = { ...st.view.roles };
          st.keepRecent = st.view.keepRecent.forced ? st.view.keepRecent.options[st.view.keepRecent.options.length - 1] : null;
          st.existing = Array.isArray(existing) ? existing : [];
          const sameTitle = st.existing.find((conv) => st.view.titleHint && conv.title === st.view.titleHint);
          st.target = sameTitle ? sameTitle.id : st.existing.length === 1 ? st.existing[0].id : NEW_CONVERSATION;
          st.title = st.view.titleHint || `${c.alias}와의 대화`;
          st.agreed = false;
          go(3);
        } catch (error) {
          if (destroyed || error.status === 401) return;
          if (isUnavailable(error)) {
            setChildren(
              unavailableBox,
              unavailable("대화 가져오기는 지금 쓸 수 없어요", "서버에서 준비되면 여기서 바로 가져올 수 있어요. 저장된 것은 없어요."),
            );
          } else if (error.status === 413) {
            showError(err, "파일이 너무 커요. 기간을 나눠 내보낸 뒤 다시 올려 주세요.");
          } else {
            showError(err, error.message || "대화를 읽지 못했어요.");
          }
        }
      });
    };

    setChildren(
      body,
      h(
        "form",
        { class: "card section", novalidate: true, onSubmit },
        stepTitle("어떻게 가져올까요?"),
        h("p", { class: "section-lead" }, h("strong", null, c.alias), `${josa(c.alias, "과/와")} 나눈 대화를 가져와요.`),
        h(
          "fieldset",
          { class: "option-list" },
          h("legend", { class: "sr-only" }, "가져오는 방법"),
          METHODS.map((m) => {
            const inputId = uid("m");
            return h(
              "div",
              { class: "option" },
              h("input", {
                type: "radio",
                id: inputId,
                name,
                value: m.value,
                checked: m.value === st.method,
                onChange: () => {
                  st.method = m.value;
                  st.file = null;
                  st.fileText = "";
                  showError(err, "");
                  renderInput();
                },
              }),
              h("label", { for: inputId }, h("span", { class: "option-title" }, m.label), h("span", { class: "option-desc" }, m.desc)),
            );
          }),
        ),
        inputBox,
        note("올린 대화는 내 계정에 저장돼요. 무엇이 저장되고 어디에 쓰이는지는 다음 단계에서 확인한 뒤 가져와요."),
        err,
        unavailableBox,
        h(
          "div",
          { class: "form-actions" },
          nextBtn,
          h("button", { type: "button", class: "btn", onClick: () => go(1) }, "이전"),
        ),
      ),
    );
    renderInput();
  }

  // ---------- ③ 미리보기 ----------
  function renderPreviewStep() {
    const c = contact();
    const v = st.view;
    if (!c || !v) {
      go(c ? 2 : 1);
      return;
    }
    const err = errorLine();
    const rolesWarn = h("p", { class: "note note-warn", hidden: true });
    const sampleBox = h("ol", { class: "chat chat-sample", "aria-label": "표본 메시지" });
    const keepInfo = h("p", { class: "muted" });
    const agreeId = uid("agree");
    const titleId = uid("title");
    const targetName = uid("target");

    function renderSample() {
      setChildren(
        sampleBox,
        v.sample.map((m) => {
          const role = st.roles[m.speaker];
          return h(
            "li",
            { class: ["chat-row", role === "me" ? "is-me" : "is-them", role === "skip" ? "is-skip" : ""] },
            h("span", { class: "chat-who" }, m.speaker, role === "me" ? " (나)" : role === "contact" ? " (상대)" : role === "skip" ? " (제외)" : ""),
            h("p", { class: "chat-bubble" }, m.text),
            h("time", { class: "chat-time" }, formatDateTime(m.sentAt)),
          );
        }),
      );
    }

    function renderKeepInfo() {
      const kept = keptMessages(v.speakers, st.roles);
      const limited = st.keepRecent ? Math.min(kept, st.keepRecent) : kept;
      keepInfo.textContent = `가져올 메시지: 약 ${formatNumber(limited)}개 (제외한 사람의 메시지는 빼고 셌어요)`;
      const check = checkRoles(st.roles);
      rolesWarn.textContent = check.ok ? check.warning || "" : "";
      rolesWarn.hidden = !(check.ok && check.warning);
    }

    const speakerRows = v.speakers.map((sp) => {
      const group = uid("role");
      return h(
        "fieldset",
        { class: "speaker" },
        h("legend", null, h("strong", null, sp.name), h("span", { class: "muted" }, ` 메시지 ${formatNumber(sp.count)}개`)),
        h(
          "div",
          { class: "seg seg-3" },
          ROLE_OPTIONS.map((opt) => {
            const inputId = uid("r");
            return [
              h("input", {
                type: "radio",
                class: "seg-input",
                id: inputId,
                name: group,
                value: opt.value,
                checked: st.roles[sp.name] === opt.value,
                onChange: () => {
                  st.roles[sp.name] = opt.value;
                  showError(err, "");
                  renderSample();
                  renderKeepInfo();
                },
              }),
              h("label", { for: inputId, class: "seg-label" }, h("span", { class: "seg-text" }, opt.label)),
            ];
          }),
        ),
      );
    });

    const keep = v.keepRecent;
    let keepField = null;
    if (keep.needed) {
      const keepId = uid("keep");
      keepField = h(
        "div",
        { class: "field" },
        h("label", { for: keepId }, "얼마나 가져올까요?"),
        h(
          "select",
          {
            id: keepId,
            class: "input",
            value: st.keepRecent ? String(st.keepRecent) : "",
            onChange: (e) => {
              st.keepRecent = e.target.value ? Number(e.target.value) : null;
              renderKeepInfo();
            },
          },
          keep.forced ? null : h("option", { value: "" }, `전체 (${formatNumber(v.total)}개)`),
          keep.options.map((n) => h("option", { value: String(n) }, `최근 ${formatNumber(n)}개만`)),
        ),
        h(
          "p",
          { class: "hint" },
          keep.forced
            ? "한 번에 가져올 수 있는 양보다 많아서 최근 대화만 가져와요. "
            : "메시지가 많아요. 최근 대화만 가져와도 말투는 충분히 읽을 수 있어요. ",
          "지난 약속·사건 정리는 메시지 하나에 10~15초쯤 걸려요.",
        ),
      );
    }

    const targetField = st.existing.length
      ? h(
          "fieldset",
          { class: "option-list" },
          h("legend", null, "어디에 저장할까요?"),
          [...st.existing.map((conv) => ({ id: conv.id, title: `기존 대화방 '${conv.title}'에 이어서`, desc: "같은 채팅방을 다시 가져오는 거라면 이쪽이에요. 이미 있는 메시지는 건너뛰어요." })), { id: NEW_CONVERSATION, title: "새 대화방으로", desc: "다른 채팅방의 대화라면 따로 저장해요." }].map((opt) => {
            const inputId = uid("t");
            return h(
              "div",
              { class: "option" },
              h("input", {
                type: "radio",
                id: inputId,
                name: targetName,
                value: opt.id,
                checked: st.target === opt.id,
                onChange: () => {
                  st.target = opt.id;
                  titleField.hidden = st.target !== NEW_CONVERSATION;
                },
              }),
              h("label", { for: inputId }, h("span", { class: "option-title" }, opt.title), h("span", { class: "option-desc" }, opt.desc)),
            );
          }),
        )
      : null;

    const titleField = h(
      "div",
      { class: "field", hidden: st.target !== NEW_CONVERSATION },
      h("label", { for: titleId }, "대화방 이름"),
      h("input", {
        id: titleId,
        class: "input",
        type: "text",
        maxlength: 200,
        value: st.title,
        onInput: (e) => {
          st.title = e.target.value;
        },
      }),
    );

    const onSubmit = (e) => {
      e.preventDefault();
      const check = checkRoles(st.roles);
      if (!check.ok) {
        showError(err, check.error);
        // 아직 정하지 않은 화자가 있으면 그 줄로, 아니면 첫 줄로 포커스를 옮긴다
        const missingAt = v.speakers.findIndex((sp) => !st.roles[sp.name]);
        const rows = body.querySelectorAll(".speaker");
        (rows[missingAt >= 0 ? missingAt : 0] || rows[0])?.querySelector("input")?.focus();
        return;
      }
      if (!st.agreed) {
        showError(err, "저장 안내를 확인했다고 체크해 주세요.");
        body.querySelector(`#${agreeId}`)?.focus();
        return;
      }
      go(4);
    };

    setChildren(
      body,
      h(
        "form",
        { class: "card section", novalidate: true, onSubmit },
        stepTitle("이렇게 읽었어요. 맞는지 확인해 주세요"),
        h(
          "dl",
          { class: "stat-row" },
          h("div", null, h("dt", null, "형식"), h("dd", null, v.formatLabel)),
          h("div", null, h("dt", null, "메시지"), h("dd", null, `${formatNumber(v.total)}개`)),
          h("div", null, h("dt", null, "기간"), h("dd", null, v.range || "—")),
          h("div", null, h("dt", null, "건너뛴 줄"), h("dd", null, `${formatNumber(v.skippedLines)}줄`)),
          h("div", null, h("dt", null, "입장·퇴장 등 알림"), h("dd", null, `${formatNumber(v.systemLines)}줄`)),
          h("div", null, h("dt", null, "가린 개인정보"), h("dd", null, `${formatNumber(v.maskedPii)}건`)),
        ),
        v.skippedLines > 0
          ? h("p", { class: "note note-warn" }, `읽지 못해 건너뛴 줄이 ${formatNumber(v.skippedLines)}줄 있어요. 그 줄은 저장되지 않아요.`)
          : null,
        v.maskedPii > 0
          ? note(`전화번호·이메일 같은 개인정보 모양 ${formatNumber(v.maskedPii)}건은 가린 채로 저장해요.`)
          : null,
        v.problems.length
          ? h(
              "div",
              { class: "warnbox", role: "note" },
              h("p", { class: "warnbox-title" }, h("span", { "aria-hidden": "true" }, "! "), "확인해 주세요"),
              h(
                "ul",
                null,
                v.problems.map((p) => h("li", null, p)),
              ),
            )
          : null,

        h("h3", { class: "section-sub" }, "누가 나인가요?"),
        h("p", { class: "hint" }, "내 메시지에서 말투를 읽어요. 단체방이라면 다른 사람은 '제외'로 두세요."),
        h("div", { class: "speaker-list" }, speakerRows),
        rolesWarn,

        h("h3", { class: "section-sub" }, `앞부분 ${formatNumber(v.sample.length)}개`),
        sampleBox,

        keepField,
        keepInfo,
        targetField,
        titleField,

        h(
          "div",
          { class: "consent-box" },
          h("h3", { class: "section-sub" }, "저장 안내"),
          h(
            "ul",
            { class: "plain-list" },
            h("li", null, "가져온 대화는 내 계정에 저장돼요. 내 말투를 찾는 검색과 지난 약속·사건 정리에 써요."),
            h("li", null, "첨삭할 때 관련된 부분이, 사건을 정리할 때는 메시지가 Anthropic의 Claude API로 전송돼요."),
            h("li", null, "언제든 상대별·대화방별로 지울 수 있어요. 지우면 원문과 그걸로 만든 자료가 함께 지워져요."),
          ),
          h(
            "div",
            { class: "check-row" },
            h("input", {
              id: agreeId,
              type: "checkbox",
              checked: st.agreed,
              onChange: (e) => {
                st.agreed = e.target.checked;
                showError(err, "");
              },
            }),
            h("label", { for: agreeId }, "위 내용을 확인했어요. ", h("a", { href: "/privacy", target: "_blank", rel: "noopener" }, "자세히 보기")),
          ),
        ),
        err,
        h(
          "div",
          { class: "form-actions" },
          h("button", { type: "submit", class: "btn btn-primary" }, "가져오기"),
          h("button", { type: "button", class: "btn", onClick: () => go(2) }, "이전"),
        ),
      ),
    );
    renderSample();
    renderKeepInfo();
  }

  // ---------- ④ 가져오기 ----------
  /**
   * 결과 화면을 한 번 그리고, 진행 상태만 바꿔 그리는 함수를 돌려준다.
   * 버튼은 다시 그리지 않는다 — 몇 초마다 통째로 바꾸면 누르는 순간 버튼이 사라질 수 있다.
   */
  function mountProgress(job) {
    const c = contact();
    const nothingNew = (job.imported ?? 0) === 0;
    const styleBox = h("div", { class: "phase phase-ok" });
    const graphBox = h("div", { class: "phase" });
    let lastStatus;

    const update = (stats, statsMissing = false) => {
      const style = stylePhaseView(job, stats);
      const graph = graphPhaseView(stats, job);
      setChildren(
        styleBox,
        h("p", { class: "phase-title" }, h("span", null, h("span", { "aria-hidden": "true" }, "✓ "), style.title)),
        h("p", { class: "phase-detail" }, style.detail),
      );
      graphBox.className = ["phase", graph.tone ? `phase-${graph.tone}` : ""].filter(Boolean).join(" ");
      setChildren(
        graphBox,
        h("p", { class: "phase-title" }, graph.title, graph.count ? h("span", { class: "phase-count" }, graph.count) : null),
        graph.pct != null && stats ? meter(stats.graph_ingested, stats.messages, "지난 약속·사건 정리 진행") : null,
        h("p", { class: "phase-detail" }, graph.detail),
        statsMissing && graph.status && graph.status !== "skipped"
          ? h("p", { class: "phase-detail muted" }, "얼마나 진행됐는지는 지금 볼 수 없어요.")
          : null,
      );
      // 숫자가 바뀔 때마다 읽어 주면 시끄럽다 — 상태가 바뀔 때만 알린다
      if (graph.status !== lastStatus) {
        lastStatus = graph.status;
        liveEl.textContent = `${style.title}. ${graph.title}${graph.count ? ` ${graph.count}` : ""}`;
      }
      return graph.polling;
    };

    setChildren(
      body,
      h(
        "section",
        { class: "card section" },
        stepTitle(nothingNew ? "새로 저장한 메시지가 없어요" : "가져왔어요"),
        nothingNew ? note("이미 가져온 대화와 같은 내용이에요. 전에 저장한 메시지는 그대로 있어요.") : null,
        styleBox,
        graphBox,
        h(
          "div",
          { class: "form-actions" },
          h("a", { class: "btn btn-primary", href: buildHash("compose", {}, { contact: c?.id }) }, "첨삭하러 가기"),
          c ? h("a", { class: "btn", href: buildHash("contact", { id: c.id }) }, "페르소나 확인하기") : null,
          h(
            "button",
            {
              type: "button",
              class: "btn btn-quiet",
              onClick: () => {
                if (pollTimer) clearTimeout(pollTimer);
                Object.assign(st, { file: null, fileText: "", pasted: "", sent: null, view: null, roles: {}, agreed: false, target: NEW_CONVERSATION });
                go(1);
              },
            },
            "다른 대화 가져오기",
          ),
        ),
      ),
    );
    update(null);
    return update;
  }

  /** 사건 정리가 끝날 때까지 통계를 다시 읽는다(처음엔 4초마다, 2분 뒤부터 10초마다). 화면을 떠나면 멈춘다. */
  function poll(update, conversationId, count = 0) {
    if (destroyed) return;
    pollTimer = setTimeout(
      async () => {
        let stats = null;
        try {
          stats = await api.conversations.stats(conversationId);
        } catch (error) {
          if (destroyed || error.status === 401) return;
          if (isMissingEndpoint(error)) {
            update(null, true);
            return; // 통계가 없는 서버: 작업 응답의 상태만 보여 주고 멈춘다
          }
          poll(update, conversationId, count + 1); // 잠깐의 오류는 다음 차례에 다시
          return;
        }
        if (destroyed || st.step !== 4) return;
        if (update(stats)) poll(update, conversationId, count + 1);
      },
      count === 0 ? 0 : count < 30 ? 4000 : 10000,
    );
  }

  async function renderRunStep() {
    const c = contact();
    const v = st.view;
    if (!c || !v || !st.sent) {
      go(1);
      return;
    }
    setChildren(
      body,
      h(
        "section",
        { class: "card section" },
        stepTitle("가져오는 중이에요"),
        skeleton(3, "대화를 저장하고 말투를 읽는 중이에요"),
        h("p", { class: "muted" }, "메시지가 많으면 조금 걸려요. 이 창을 그대로 두세요."),
      ),
    );
    let conversationId = st.target === NEW_CONVERSATION ? null : st.target;
    let created = false;
    try {
      if (!conversationId) {
        const conv = await api.conversations.create({
          contact_id: c.id,
          title: st.title.trim() || null,
          source: st.method,
          me_name: meName(v.speakers, st.roles),
        });
        conversationId = conv.id;
        created = true;
      }
      const payload = { format: v.format, text: st.sent.text, speaker_map: { ...st.roles }, tz_offset_minutes: st.sent.tz_offset_minutes };
      if (st.keepRecent) payload.keep_recent = st.keepRecent;
      const job = await api.conversations.importRaw(conversationId, payload);
      if (destroyed) return;
      st.target = conversationId; // 같은 화면에서 다시 시도해도 대화방을 또 만들지 않는다
      const update = mountProgress(job);
      body.querySelector("h2")?.focus();
      poll(update, conversationId);
    } catch (error) {
      if (created && conversationId) api.conversations.remove(conversationId).catch(() => {}); // 빈 대화방을 남기지 않는다
      if (destroyed || error.status === 401) return;
      const missing = isUnavailable(error);
      setChildren(
        body,
        h(
          "section",
          { class: "card section" },
          stepTitle("가져오지 못했어요"),
          missing
            ? unavailable("대화 가져오기는 지금 쓸 수 없어요", "서버에서 준비되면 다시 시도해 주세요. 저장된 것은 없어요.")
            : errorState(error.message || "잠시 뒤 다시 시도해 주세요.", () => go(4)),
          h(
            "div",
            { class: "form-actions" },
            h("button", { type: "button", class: "btn", onClick: () => go(3) }, "미리보기로 돌아가기"),
          ),
        ),
      );
      body.querySelector("h2")?.focus();
    }
  }

  // ---------- 시작 ----------
  const start = () => {
    renderStepper();
    setChildren(body, h("div", { class: "card section" }, skeleton(3)));
    loadContacts()
      .then(() => {
        if (destroyed) return;
        if (st.contactId && !contact()) st.contactId = null;
        st.step = st.contactId ? 2 : 1;
        renderStepper();
        if (st.step === 2) renderMethodStep();
        else renderContactStep();
      })
      .catch((error) => {
        if (destroyed || error.status === 401) return;
        setChildren(body, errorState(error.message, start));
      });
  };
  start();

  return () => {
    destroyed = true;
    if (pollTimer) clearTimeout(pollTimer);
  };
}
