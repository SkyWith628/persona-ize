import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_IMPORT_CHARS,
  chatTypeLabel,
  checkRoles,
  checkSize,
  decodeBytes,
  encodingLabel,
  formatLabel,
  POLL_STALL_MS,
  graphPhaseView,
  graphStatus,
  isPlaceholderName,
  keepRecentOptions,
  keptMessages,
  meName,
  previewView,
  stylePhaseView,
  suggestRoles,
  trackProgress,
  utf8Bytes,
} from "../js/lib/importing.js";

const SAMPLE = "2026년 10월 5일 오전 9:25, 박지현 : 민수씨, 개편은 언제까지 가능해요?\n";

function utf16(text, littleEndian, bom) {
  const out = [];
  if (bom) out.push(...(littleEndian ? [0xff, 0xfe] : [0xfe, 0xff]));
  for (let i = 0; i < text.length; i += 1) {
    const c = text.charCodeAt(i);
    out.push(...(littleEndian ? [c & 0xff, c >> 8] : [c >> 8, c & 0xff]));
  }
  return new Uint8Array(out);
}

function canDecode(label) {
  try {
    new TextDecoder(label, { fatal: true });
    return true;
  } catch {
    return false;
  }
}

test("UTF-8 파일을 그대로 읽는다", () => {
  const r = decodeBytes(new TextEncoder().encode(SAMPLE));
  assert.equal(r.encoding, "utf-8");
  assert.equal(r.text, SAMPLE);
  assert.equal(r.replaced, 0);
});

test("UTF-8 BOM은 떼고 읽는다", () => {
  const body = new TextEncoder().encode(SAMPLE);
  const r = decodeBytes(new Uint8Array([0xef, 0xbb, 0xbf, ...body]));
  assert.equal(r.encoding, "utf-8");
  assert.equal(r.text, SAMPLE);
});

test("ArrayBuffer로 줘도 읽는다", () => {
  const r = decodeBytes(new TextEncoder().encode("가나다").buffer);
  assert.equal(r.text, "가나다");
});

test("UTF-16(BOM 있음)을 읽는다", { skip: !canDecode("utf-16le") }, () => {
  const r = decodeBytes(utf16(SAMPLE, true, true));
  assert.equal(r.encoding, "utf-16le");
  assert.equal(r.text, SAMPLE);
});

test("UTF-16 빅엔디언(BOM 있음)을 읽는다", { skip: !canDecode("utf-16be") }, () => {
  const r = decodeBytes(utf16(SAMPLE, false, true));
  assert.equal(r.encoding, "utf-16be");
  assert.equal(r.text, SAMPLE);
});

test("BOM 없는 UTF-16도 0 바이트 위치로 알아본다", { skip: !canDecode("utf-16le") }, () => {
  const r = decodeBytes(utf16(SAMPLE, true, false));
  assert.equal(r.encoding, "utf-16le");
  assert.equal(r.text, SAMPLE);
});

test("EUC-KR(완성형) 파일은 UTF-8이 깨지면 다시 읽는다", { skip: !canDecode("euc-kr") }, () => {
  // "안녕하세요" 의 EUC-KR 바이트
  const bytes = new Uint8Array([0xbe, 0xc8, 0xb3, 0xe7, 0xc7, 0xcf, 0xbc, 0xbc, 0xbf, 0xe4]);
  const r = decodeBytes(bytes);
  assert.equal(r.encoding, "euc-kr");
  assert.equal(r.text, "안녕하세요");
  assert.equal(r.replaced, 0);
});

test("인코딩 시도 순서: UTF-8 → EUC-KR → 마지막 수단(깨진 글자 수 보고)", () => {
  const tried = [];
  const decode = (label, _bytes, fatal) => {
    tried.push(`${label}${fatal ? "!" : ""}`);
    if (fatal) throw new TypeError("깨짐");
    return "가�나�";
  };
  const r = decodeBytes(new Uint8Array([0xbe, 0xc8, 0x41]), decode);
  assert.deepEqual(tried, ["utf-8!", "euc-kr!", "utf-8"]);
  assert.equal(r.encoding, "utf-8");
  assert.equal(r.replaced, 2);
});

test("인코딩 이름을 사람이 읽는 말로", () => {
  assert.equal(encodingLabel("euc-kr"), "EUC-KR(완성형)");
  assert.equal(encodingLabel("utf-16le"), "UTF-16");
});

test("보내기 전 크기 확인: 빈 내용·글자 수·본문 크기", () => {
  assert.match(checkSize("   "), /비어/);
  assert.equal(checkSize(SAMPLE), null);
  assert.match(checkSize("a".repeat(MAX_IMPORT_CHARS + 1)), /글자가 너무 많아요/);
  // 한글은 3바이트지만 본문 상한(8MB) 안이다: 100만 자 = 3MB
  const korean = "가".repeat(1_000_000);
  assert.equal(utf8Bytes(korean), 3_000_000);
  assert.equal(checkSize(korean), null);
  // 글자 수 한도 안이어도 4바이트 글자뿐이면 본문 크기 한도를 넘는다
  const emoji = "😊".repeat(MAX_IMPORT_CHARS);
  assert.equal(utf8Bytes(emoji), 8_000_000);
  assert.match(checkSize(emoji), /파일이 너무 커요/);
});

// 계약 §4.4: POST /v1/imports/parse 응답 예시
const PREVIEW = {
  format: "kakao",
  variant: "android",
  title_hint: "박지현",
  messages_total: 312,
  date_from: "2026-10-01T09:00:00+09:00",
  date_to: "2026-10-31T21:10:00+09:00",
  speakers: [
    { name: "김민수", count: 168, suggested_role: "me" },
    { name: "박지현", count: 144, suggested_role: "contact" },
  ],
  sample: [{ id: "k-1", speaker: "박지현", sent_at: "2026-10-01T09:00:00+09:00", text: "민수씨, 개편은 언제까지 가능해요?" }],
  skipped_lines: 3,
  system_lines: 5,
  masked_pii: 2,
  problems: ["3번째 줄을 읽지 못했습니다."],
};

test("미리보기 응답을 화면용으로 정리한다", () => {
  const v = previewView(PREVIEW, "박지현");
  assert.equal(v.formatLabel, "카카오톡 (안드로이드)");
  assert.equal(v.total, 312);
  assert.equal(v.titleHint, "박지현");
  assert.deepEqual(v.roles, { 김민수: "me", 박지현: "contact" });
  assert.equal(v.sample[0].speaker, "박지현");
  assert.equal(v.skippedLines, 3);
  assert.equal(v.systemLines, 5);
  assert.equal(v.maskedPii, 2);
  assert.deepEqual(v.problems, ["3번째 줄을 읽지 못했습니다."]);
  assert.equal(v.keepRecent.needed, false);
  assert.match(v.range, /^\d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}$/);
});

test("미리보기: 한 번에 가져올 수 있는 수(max_messages)를 넘으면 최근 것만 고르게 한다", () => {
  const v = previewView({ ...PREVIEW, messages_total: 7200, max_messages: 5000 }, "박지현");
  assert.equal(v.maxMessages, 5000);
  assert.deepEqual(v.keepRecent, { needed: true, options: [500, 1000, 3000, 5000], forced: true, max: 5000 });
  assert.equal(previewView(PREVIEW).maxMessages, 5000, "응답에 없으면 기본값");
});

test("미리보기: 텔레그램 계정 전체 내보내기는 대화 목록을 먼저 고르게 한다", () => {
  // 계약(2단계): messages_total 0 + chats 목록 + problems 안내 → chat_id로 다시 parse
  const v = previewView({
    format: "telegram",
    variant: null,
    title_hint: null,
    messages_total: 0,
    date_from: null,
    date_to: null,
    speakers: [],
    sample: [],
    skipped_lines: 0,
    system_lines: 0,
    masked_pii: 0,
    problems: ["계정 전체 내보내기입니다. 대화 3개 중 가져올 하나를 골라 주세요 (chat_id)."],
    max_messages: 5000,
    chats: [
      { id: "777", name: "박지현", type: "personal_chat", messages: 5 },
      { id: "888", name: "최유진", type: "personal_chat", messages: 1 },
      { id: "1", name: "빈 방", type: "private_group", messages: 0 },
    ],
  });
  assert.equal(v.needsChat, true);
  assert.deepEqual(v.chats, [
    { id: "777", name: "박지현", typeLabel: "1:1 대화", messages: 5 },
    { id: "888", name: "최유진", typeLabel: "1:1 대화", messages: 1 },
    { id: "1", name: "빈 방", typeLabel: "그룹", messages: 0 },
  ]);
  assert.equal(v.formatLabel, "텔레그램");
  assert.equal(previewView(PREVIEW).needsChat, false);
  assert.equal(chatTypeLabel("something_new"), "something_new");
  assert.equal(chatTypeLabel(null), "");
});

test("미리보기에 빠진 값이 있어도 죽지 않는다", () => {
  const v = previewView({ format: "plain", variant: null, messages_total: 2, speakers: [{ name: "me", count: 2, suggested_role: null }] });
  assert.equal(v.formatLabel, "붙여넣기(줄 형식)");
  assert.deepEqual(v.sample, []);
  assert.deepEqual(v.problems, []);
  assert.equal(v.range, "");
  assert.equal(previewView(null).total, 0);
});

test("형식 이름", () => {
  assert.equal(formatLabel("kakao", "csv"), "카카오톡 (PC · CSV)");
  assert.equal(formatLabel("telegram", null), "텔레그램");
});

test("화자 미리 채우기: 서버 추천이 없으면 상대 이름으로, 둘이면 나머지는 나", () => {
  const speakers = [
    { name: "김민수", count: 5, suggested_role: null },
    { name: "박지현 팀장", count: 4, suggested_role: null },
  ];
  assert.deepEqual(suggestRoles(speakers, "박지현"), { 김민수: "me", "박지현 팀장": "contact" });
});

test("화자 미리 채우기: 단체방은 모르는 사람을 비워 둔다", () => {
  const speakers = [
    { name: "김민수", count: 5, suggested_role: "me" },
    { name: "박지현", count: 4, suggested_role: null },
    { name: "이서연", count: 3, suggested_role: null },
  ];
  assert.deepEqual(suggestRoles(speakers, "박지현"), { 김민수: "me", 박지현: "contact", 이서연: null });
});

test("화자 미리 채우기: 이름이 안 맞으면 비워 두고, 이상한 추천값은 무시한다", () => {
  const speakers = [
    { name: "A", count: 1, suggested_role: "boss" },
    { name: "B", count: 1 },
  ];
  assert.deepEqual(suggestRoles(speakers, "박지현"), { A: null, B: null });
  assert.deepEqual(suggestRoles([], "박지현"), {});
});

test("화자 미리 채우기: 붙여넣기의 me·contact가 섞여 있어도 이름으로 나머지를 채운다", () => {
  const speakers = [
    { name: "최유진", count: 1, suggested_role: null },
    { name: "김민수", count: 1, suggested_role: null },
    { name: "me", count: 1, suggested_role: "me" },
    { name: "contact", count: 1, suggested_role: "contact" },
  ];
  // 상대 이름과 같은 화자는 상대, 이 상대의 다른 대화방에서 '나'였던 이름은 나
  assert.deepEqual(suggestRoles(speakers, "최유진", ["김민수", null]), { 최유진: "contact", 김민수: "me", me: "me", contact: "contact" });
  // 내 이름을 모르면 그 사람은 비워 둔다(사용자가 고른다)
  assert.deepEqual(suggestRoles(speakers, "최유진"), { 최유진: "contact", 김민수: null, me: "me", contact: "contact" });
  // 미리보기 화면용 값에도 그대로 반영된다
  const view = previewView({ speakers, messages_total: 4 }, "최유진", ["김민수"]);
  assert.equal(view.roles["김민수"], "me");
});

test("화자 지정 검사", () => {
  assert.equal(checkRoles({ 김민수: "me", 박지현: "contact" }).ok, true);
  assert.match(checkRoles({ 김민수: "me", 박지현: null }).error, /박지현/);
  assert.match(checkRoles({ 김민수: "contact", 박지현: "skip" }).error, /'나'를 한 명 이상/);
  const onlyMe = checkRoles({ 김민수: "me", 박지현: "skip" });
  assert.equal(onlyMe.ok, true);
  assert.match(onlyMe.warning, /'상대'로 지정한 화자가 없어요/);
  assert.equal(checkRoles({}).ok, false);
});

test("내 이름과 저장될 메시지 수", () => {
  const speakers = [
    { name: "민수", count: 3 },
    { name: "김민수", count: 168 },
    { name: "박지현", count: 144 },
    { name: "광고봇", count: 9 },
  ];
  const roles = { 민수: "me", 김민수: "me", 박지현: "contact", 광고봇: "skip" };
  assert.equal(meName(speakers, roles), "김민수");
  assert.equal(keptMessages(speakers, roles), 315);
  assert.equal(meName(speakers, {}), null);
});

test("최근 N개만: 많을 때만 내놓고, 한도를 넘으면 반드시 골라야 한다", () => {
  assert.deepEqual(keepRecentOptions(312), { needed: false, options: [], forced: false, max: 5000 });
  assert.deepEqual(keepRecentOptions(2400), { needed: true, options: [500, 1000], forced: false, max: 5000 });
  assert.deepEqual(keepRecentOptions(12000), { needed: true, options: [500, 1000, 3000, 5000], forced: true, max: 5000 });
  // 서버가 알려 준 한 번에 가져올 수 있는 수(max_messages)를 쓴다
  assert.deepEqual(keepRecentOptions(12000, 2000), { needed: true, options: [500, 1000, 2000], forced: true, max: 2000 });
  assert.deepEqual(keepRecentOptions(800, 600), { needed: true, options: [500, 600], forced: true, max: 600 });
  assert.equal(keepRecentOptions(12000, 0).max, 5000, "응답에 값이 없으면 기본값");
});

// 계약 §4.4: GET /v1/conversations/{id}/stats
const JOB = { id: "j1", conversation_id: "v1", status: "completed", imported: 312, duplicates: 0, episodes: 57, error_code: null, error_message: null, graph_status: "running", graph_episodes: 38, graph_error: null, created_at: "2026-11-11T02:00:00Z" };
const STATS = { conversation_id: "v1", messages: 312, my_messages: 168, episodes: 57, graph_enabled: true, graph_ingested: 38, graph_pending: 274, graph_limited: false, first_message_at: "2026-10-01T00:00:00Z", last_message_at: "2026-10-31T12:10:00Z", latest_job: JOB };

test("1단계: 말투 분석은 바로 끝난다", () => {
  const v = stylePhaseView(JOB, STATS);
  assert.equal(v.title, "말투 분석 완료 · 지금 바로 쓸 수 있어요");
  assert.equal(v.detail, "메시지 312개 중 내 메시지 168개 · 말투 묶음 57개");
  assert.equal(stylePhaseView({ ...JOB, imported: 10, duplicates: 2 }, null).detail, "메시지 10개 저장 · 이미 있던 2개는 건너뜀 · 말투 묶음 57개");
});

test("2단계: 사건 정리 중 38 / 312", () => {
  const v = graphPhaseView(STATS, JOB);
  assert.equal(v.status, "running");
  assert.equal(v.title, "지난 약속·사건 정리 중");
  assert.equal(v.count, "38 / 312");
  assert.equal(v.pct, 12);
  assert.equal(v.polling, true);
});

test("2단계: 상태별 문구와 폴링 여부", () => {
  const withStatus = (s, extra = {}) => graphPhaseView({ ...STATS, ...extra, latest_job: { ...JOB, graph_status: s } }, JOB);
  assert.equal(withStatus("pending").polling, true);
  assert.match(withStatus("pending").title, /대기 중/);
  const done = withStatus("completed", { graph_ingested: 312, graph_pending: 0 });
  assert.equal(done.polling, false);
  assert.equal(done.count, "312 / 312");
  assert.equal(done.tone, "ok");
  const failed = graphPhaseView({ ...STATS, latest_job: { ...JOB, graph_status: "failed", graph_error: "RateLimitError" } }, JOB);
  assert.equal(failed.polling, false);
  assert.match(failed.detail, /RateLimitError/);
  assert.equal(failed.tone, "bad");
});

test("2단계: 그래프를 쓰지 않는 서버와 한도에 닿은 경우", () => {
  const skipped = graphPhaseView({ ...STATS, graph_enabled: false, latest_job: { ...JOB, graph_status: "skipped" } }, JOB);
  assert.equal(skipped.status, "skipped");
  assert.equal(skipped.polling, false);
  assert.equal(skipped.pct, null);
  const limited = graphPhaseView({ ...STATS, graph_limited: true, latest_job: { ...JOB, graph_status: "limited" } }, JOB);
  assert.equal(limited.status, "limited");
  assert.equal(limited.count, "38 / 312");
  assert.match(limited.title, /한도/);
  assert.equal(limited.polling, false, "limited는 끝난 상태 — 더 읽지 않는다");
  const daily = graphPhaseView({ ...STATS, graph_limited: true, latest_job: { ...JOB, graph_status: "limited", graph_error: "daily_cap" } }, JOB);
  assert.match(daily.detail, /오늘 서비스 전체/);
  const user = graphPhaseView({ ...STATS, graph_limited: true, latest_job: { ...JOB, graph_status: "limited", graph_error: "user_cap" } }, JOB);
  assert.match(user.detail, /계정마다/);
});

test("2단계: 통계를 못 읽으면 작업 응답의 상태만 쓴다", () => {
  assert.equal(graphStatus(null, JOB), "running");
  const v = graphPhaseView(null, { ...JOB, graph_status: "skipped" });
  assert.equal(v.status, "skipped");
  const unknown = graphPhaseView(null, null);
  assert.equal(unknown.status, null);
  assert.equal(unknown.polling, false);
  assert.match(unknown.title, /지금은 볼 수 없어요/);
});

test("2단계: 작업 정보가 없어도 통계 숫자로 상태를 정한다", () => {
  assert.equal(graphStatus({ ...STATS, latest_job: null }, null), "running");
  assert.equal(graphStatus({ ...STATS, latest_job: null, graph_pending: 0, graph_ingested: 312 }, null), "completed");
});

test("내 이름: 내보내기의 자리표시 이름(회원님 등)은 보내지 않는다", () => {
  assert.ok(isPlaceholderName("회원님"));
  assert.ok(isPlaceholderName(" Me "));
  assert.ok(!isPlaceholderName("김민수"));
  // 카카오톡 안드로이드: 나는 "회원님"으로만 나온다 → 내 이름을 모른다
  assert.equal(meName([{ name: "회원님", count: 5 }, { name: "박지현", count: 3 }], { 회원님: "me", 박지현: "contact" }), null);
  // 자리표시와 진짜 이름이 함께 '나'면 진짜 이름을 쓴다(메시지 수가 적어도)
  assert.equal(
    meName([{ name: "me", count: 9 }, { name: "김민수", count: 2 }, { name: "최유진", count: 4 }], { me: "me", 김민수: "me", 최유진: "contact" }),
    "김민수",
  );
});

test("사건 정리 상태: 작업 기록보다 진행 수치를 먼저 본다(서버 재시작 뒤 running으로 남는 경우)", () => {
  const stale = { ...STATS, graph_ingested: 312, graph_pending: 0, latest_job: { ...JOB, graph_status: "running" } };
  assert.equal(graphStatus(stale, JOB), "completed");
  assert.equal(graphPhaseView(stale, JOB).polling, false);
  // 한도 표시는 작업 기록과 수치보다 먼저다
  assert.equal(graphStatus({ ...stale, graph_limited: true }, JOB), "limited");
  // 아직 남았으면 작업 기록을 따른다
  assert.equal(graphStatus({ ...STATS, latest_job: { ...JOB, graph_status: "pending" } }, JOB), "pending");
});

test("사건 정리 진행이 몇 분째 그대로면 더 읽지 않는다", () => {
  const t0 = 1_000_000;
  let p = trackProgress(null, STATS, t0);
  assert.deepEqual(p, { key: "38/312", since: t0, stalled: false });
  p = trackProgress(p, STATS, t0 + POLL_STALL_MS - 1);
  assert.equal(p.stalled, false);
  p = trackProgress(p, STATS, t0 + POLL_STALL_MS);
  assert.equal(p.stalled, true);
  const view = graphPhaseView(STATS, JOB, { stalled: p.stalled });
  assert.equal(view.polling, false);
  assert.match(view.title, /멈춰/);
  assert.match(view.detail, /나중에/);
  // 수치가 움직이면 시계를 새로 잡는다
  const moved = trackProgress(p, { ...STATS, graph_ingested: 39 }, t0 + POLL_STALL_MS + 5);
  assert.deepEqual(moved, { key: "39/312", since: t0 + POLL_STALL_MS + 5, stalled: false });
  // 끝난 상태에는 멈춤 표시를 덮어쓰지 않는다
  const done = graphPhaseView({ ...STATS, graph_ingested: 312, graph_pending: 0 }, JOB, { stalled: true });
  assert.equal(done.status, "completed");
  assert.match(done.title, /완료/);
});
