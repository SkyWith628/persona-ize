// 화자 역할 고치기(나 ↔ 상대 맞바꾸기)의 순수 로직. 화면은 views/conversations.js.
//
// 서버에는 메시지마다 원래 화자 이름이 없고 역할(me/contact)만 있다 — 고칠 수 있는 것은 대화방 전체를
// 맞바꾸는 것뿐이다. 요청은 "이 메시지는 ○○가 한 말"이라고 맞는 상태를 적는다. 그래서 같은 요청이 두 번
// 가도(두 번 누름, 다시 시도) 도로 뒤집히지 않는다.

const OTHER = { me: "contact", contact: "me" };
export const ME_NAME_MAX = 50; // 서버의 me_name 길이 제한

/**
 * 맞바꾸기 요청 본문. 지금 보이는 첫 메시지를 기준으로 "반대쪽이 한 말"이라고 적는다.
 * @param {Array<{id: string, speaker_role: string}>} messages
 * @param {string} [meName] 그래프 기록에 쓸 내 이름(선택)
 * @returns {{message_id: string, speaker_role: string, me_name?: string} | null} 메시지가 없으면 null
 */
export function swapRequest(messages, meName = "") {
  const anchor = (messages || []).find((m) => m && m.id && OTHER[m.speaker_role]);
  if (!anchor) return null;
  const body = { message_id: anchor.id, speaker_role: OTHER[anchor.speaker_role] };
  const name = String(meName || "").trim();
  if (name) body.me_name = name.slice(0, ME_NAME_MAX);
  return body;
}

/**
 * 확인 창에 보여 줄 미리보기: 바꾸면 누가 한 말이 되는지.
 * @returns {{mineNow: number, mineAfter: number, sample: Array<{now: string, after: string, text: string}>}}
 */
export function swapPreview(messages, contactAlias, limit = 3) {
  const list = (messages || []).filter((m) => m && OTHER[m.speaker_role]);
  const mineNow = list.filter((m) => m.speaker_role === "me").length;
  const who = (role) => (role === "me" ? "나" : contactAlias || "상대");
  return {
    mineNow,
    mineAfter: list.length - mineNow,
    sample: list.slice(-limit).map((m) => ({ now: who(m.speaker_role), after: who(OTHER[m.speaker_role]), text: m.text })),
  };
}

/**
 * 확인 창의 안내 줄. 그래프(지난 약속·사건 정리)가 켜진 서버에서는 다시 정리하느라 시간이 걸린다고 알린다.
 * @param {{graph_enabled?: boolean, graph_ingested?: number} | null} stats
 */
export function swapNotes(stats) {
  const notes = ["메시지는 지워지지 않아요. 누가 한 말인지만 바뀌어요.", "말투 자료는 내 메시지로 다시 만들어요. 대화가 길면 조금 걸려요."];
  if (stats && stats.graph_enabled) {
    const done = Number(stats.graph_ingested) || 0;
    notes.push(
      done > 0
        ? `지난 약속·사건 정리를 다시 해야 해서 시간이 걸려요. 이미 정리한 ${done.toLocaleString("ko-KR")}개는 지우고 처음부터 다시 정리해요.`
        : "지난 약속·사건 정리를 다시 해야 해서 시간이 걸려요.",
    );
  }
  return notes;
}

/** 내 이름 칸을 보여 줄 것인가 — 이름은 지난 약속·사건 기록에만 쓰인다. */
export function asksMeName(stats) {
  return Boolean(stats && stats.graph_enabled);
}

/**
 * 끝난 뒤 알림 문구.
 * @param {{changed: boolean, my_messages: number, job: object|null}} result PUT …/speaker-roles 응답
 * @returns {{text: string, kind: string}}
 */
export function swapResultView(result) {
  if (!result || !result.changed) return { text: "이미 바뀌어 있어요. 바꾼 것은 없어요.", kind: "info" };
  const mine = (Number(result.my_messages) || 0).toLocaleString("ko-KR");
  const job = result.job || {};
  let text = `나와 상대를 맞바꿨어요 — 이제 내 메시지는 ${mine}개예요.`;
  text += job.status === "completed" ? " 말투 자료도 다시 만들었어요." : " 말투 자료를 다시 만들고 있어요.";
  if (job.graph_status && job.graph_status !== "skipped") text += " 지난 약속·사건은 다시 정리하고 있어요. 시간이 걸려요.";
  return { text, kind: "ok" };
}

/**
 * 이 기능에서 나는 오류 가운데 화면 말투로 따로 알릴 것. 해당 없으면 null(공통 문구를 쓴다).
 * @param {{status?: number, code?: string}} err
 */
export function swapErrorText(err) {
  if (!err) return null;
  if (err.status === 409) return "지난 약속·사건 정리가 바로 멈추지 않아 바꾸지 못했어요. 잠시 뒤에 다시 해 주세요.";
  if (err.status === 503 && err.code === "graph_unavailable") return "지난 약속·사건 기록에 닿지 못해 바꾸지 않았어요. 잠시 뒤 다시 해 주세요.";
  if (err.status === 404) return "대화방이 그 사이 바뀌었어요. 화면을 새로 고친 뒤 다시 해 주세요.";
  return null;
}
