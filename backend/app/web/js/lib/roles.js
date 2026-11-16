// 화자 역할 고치기의 순수 로직. 화면은 views/conversations.js.
//
// 두 가지다: 대화방 전체의 나 ↔ 상대 맞바꾸기(아래), 화자 이름별로 고치기(파일 끝 — 서버가 원래 화자
// 이름을 남긴 대화방에서만). 맞바꾸기 요청은 "이 메시지는 ○○가 한 말"이라고 맞는 상태를 적는다. 그래서 같은 요청이 두 번
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

// ---------- 화자별로 고치기 ----------
//
// 서버가 메시지마다 원래 화자 이름을 남긴 대화방에서는(GET …/speakers의 name이 있는 줄) 화자 한 명씩
// 나/상대/제외를 다시 정할 수 있다. 요청은 달라진 화자만, 맞는 상태로 적는다 — 다시 보내도 결과가 같다.

const ROLES = ["me", "contact"];

/**
 * 이름이 있는 화자만, 이름 하나에 한 줄로. 한 이름에 두 역할이 섞여 있으면 role은 null(다시 정해야 한다).
 * @param {Array<{name: string|null, speaker_role: string, messages: number}>} speakers GET …/speakers 응답
 * @returns {Array<{name: string, role: string|null, messages: number}>}
 */
export function namedSpeakers(speakers) {
  const byName = new Map();
  for (const sp of speakers || []) {
    if (!sp || !sp.name || !ROLES.includes(sp.speaker_role)) continue;
    const row = byName.get(sp.name) || { name: sp.name, role: sp.speaker_role, messages: 0 };
    if (row.role !== sp.speaker_role) row.role = null;
    row.messages += Number(sp.messages) || 0;
    byName.set(sp.name, row);
  }
  return [...byName.values()];
}

/** 이름 없이 저장된 메시지 수 — 화자별로 고칠 수 없다(같은 파일을 다시 가져오면 이름이 채워진다). */
export function unnamedMessages(speakers) {
  return (speakers || []).filter((sp) => sp && !sp.name).reduce((sum, sp) => sum + (Number(sp.messages) || 0), 0);
}

/** 화자별로 고칠 수 있는 대화방인가 — 이름이 남아 있는 화자가 한 명이라도 있어야 한다. */
export function canFixBySpeaker(speakers) {
  return namedSpeakers(speakers).length > 0;
}

/** 말풍선에 화자 이름을 보여 줄 것인가 — 상대 쪽 화자가 둘 이상인 방(단체방)에서만. */
export function showsSpeakerNames(speakers) {
  return namedSpeakers(speakers).filter((sp) => sp.role !== "me").length > 1;
}

/**
 * 고른 역할로 무엇이 달라지는지.
 * @param {Array} speakers GET …/speakers 응답
 * @param {Record<string, string>} chosen 이름 → "me" | "contact" | "skip"
 * @returns {{body: {speakers: Record<string, string>}|null, removed: number, removedNames: string[], emptied: boolean}}
 *   body가 null이면 달라진 것이 없다. emptied면 메시지가 하나도 남지 않아 보낼 수 없다.
 */
export function speakerRolesPlan(speakers, chosen) {
  const rows = namedSpeakers(speakers);
  const changes = {};
  const removedNames = [];
  let removed = 0;
  for (const sp of rows) {
    const want = chosen ? chosen[sp.name] : undefined;
    if (want === "skip") {
      changes[sp.name] = "skip";
      removedNames.push(sp.name);
      removed += sp.messages;
    } else if (ROLES.includes(want) && want !== sp.role) {
      changes[sp.name] = want;
    }
  }
  const total = rows.reduce((sum, sp) => sum + sp.messages, 0) + unnamedMessages(speakers);
  return {
    body: Object.keys(changes).length ? { speakers: changes } : null,
    removed,
    removedNames,
    emptied: total > 0 && removed >= total,
  };
}

/**
 * 끝난 뒤 알림 문구.
 * @param {{changed: boolean, messages: number, messages_removed: number, my_messages: number, job: object|null}} result
 */
export function speakerRolesResultView(result) {
  if (!result || !result.changed) return { text: "이미 그렇게 되어 있어요. 바꾼 것은 없어요.", kind: "info" };
  const n = (v) => (Number(v) || 0).toLocaleString("ko-KR");
  const parts = [];
  if (Number(result.messages) > 0) parts.push(`메시지 ${n(result.messages)}개의 화자를 바꿨어요`);
  if (Number(result.messages_removed) > 0) parts.push(`제외한 사람의 메시지 ${n(result.messages_removed)}개를 지웠어요`);
  const job = result.job || {};
  let text = `${parts.join(", ") || "화자를 고쳤어요"} — 이제 내 메시지는 ${n(result.my_messages)}개예요.`;
  text += job.status === "completed" ? " 말투 자료도 다시 만들었어요." : " 말투 자료를 다시 만들고 있어요.";
  if (job.graph_status && job.graph_status !== "skipped") text += " 지난 약속·사건은 바뀐 부분을 다시 정리하고 있어요.";
  return { text, kind: "ok" };
}
