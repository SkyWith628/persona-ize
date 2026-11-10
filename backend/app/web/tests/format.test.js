import assert from "node:assert/strict";
import { test } from "node:test";

import {
  clip,
  countCodePoints,
  dayKey,
  formatBytes,
  formatDateTime,
  formatDay,
  formatNumber,
  formatRange,
  formatSeconds,
  formatTime,
  initial,
  isMacLike,
  josa,
  percent,
  uuid4,
  withJosa,
} from "../js/lib/format.js";

const KST = { offsetMinutes: 540 };

test("시각을 한국 시간으로 적는다", () => {
  const now = new Date("2026-11-11T03:00:00Z");
  assert.equal(formatDateTime("2026-10-05T09:31:00+09:00", { ...KST, now }), "10월 5일 09:31");
  assert.equal(formatDateTime("2026-10-05T00:31:00Z", { ...KST, now }), "10월 5일 09:31");
  assert.equal(formatTime("2026-10-05T09:05:00+09:00", KST), "09:05");
});

test("올해가 아니면 연도를 붙인다", () => {
  const now = new Date("2026-11-11T03:00:00Z");
  assert.equal(formatDateTime("2025-12-31T23:59:00+09:00", { ...KST, now }), "2025년 12월 31일 23:59");
});

test("날짜 경계는 시간대 기준이다", () => {
  // UTC로는 10월 4일 15:30이지만 한국에서는 10월 5일 00:30
  assert.equal(dayKey("2026-10-04T15:30:00Z", KST), "2026-10-05");
  assert.equal(formatDay("2026-10-04T15:30:00Z", KST), "2026년 10월 5일 (월)");
  assert.equal(dayKey("2026-10-04T15:30:00Z", { offsetMinutes: 0 }), "2026-10-04");
});

test("기간 표시", () => {
  assert.equal(formatRange("2026-10-01T09:00:00+09:00", "2026-10-31T21:00:00+09:00", KST), "2026-10-01 ~ 2026-10-31");
  assert.equal(formatRange("2026-10-01T09:00:00+09:00", "2026-10-01T21:00:00+09:00", KST), "2026-10-01");
  assert.equal(formatRange(null, null, KST), "");
});

test("잘못된 시각은 빈 글자", () => {
  assert.equal(formatDateTime("이상한 값", KST), "");
  assert.equal(formatDay(undefined, KST), "");
});

test("수 표기", () => {
  assert.equal(formatNumber(0), "0");
  assert.equal(formatNumber(1234567), "1,234,567");
  assert.equal(formatNumber(-1234), "-1,234");
  assert.equal(formatNumber(null), "—");
  assert.equal(formatNumber("abc"), "—");
  assert.equal(formatSeconds(4400), "4.4초");
  assert.equal(formatSeconds(820), "0.8초");
  assert.equal(formatBytes(512), "512B");
  assert.equal(formatBytes(1536), "1.5KB");
  assert.equal(formatBytes(3 * 1024 * 1024), "3.0MB");
});

test("글자 수는 서버(Python)와 같은 기준으로 센다", () => {
  assert.equal(countCodePoints("안녕하세요"), 5);
  assert.equal(countCodePoints("좋아요 😊"), 5); // 이모지는 1글자
  assert.equal("좋아요 😊".length, 6);
  assert.equal(countCodePoints(""), 0);
});

test("비율", () => {
  assert.equal(percent(38, 312), 12);
  assert.equal(percent(0, 0), 0);
  assert.equal(percent(400, 312), 100);
});

test("조사를 받침에 맞게 고른다", () => {
  assert.equal(withJosa("박지현", "을/를"), "박지현을");
  assert.equal(withJosa("김준호", "을/를"), "김준호를");
  assert.equal(withJosa("최유진", "과/와"), "최유진과");
  assert.equal(withJosa("이서연", "이/가"), "이서연이");
  assert.equal(withJosa("김준호", "이/가"), "김준호가");
  assert.equal(josa("서울", "으로/로"), "로");
  assert.equal(josa("부산", "으로/로"), "으로");
  assert.equal(withJosa("Alex", "을/를"), "Alex을(를)", "한글로 끝나지 않으면 둘 다 적는다");
  assert.equal(josa("", "은/는"), "은(는)");
});

test("이름 첫 글자와 플랫폼", () => {
  assert.equal(initial("박지현"), "박");
  assert.equal(initial("  "), "?");
  assert.ok(isMacLike("MacIntel"));
  assert.ok(!isMacLike("Win32"));
});

test("uuid4는 randomUUID가 없어도 v4 모양을 만든다", () => {
  const fake = { getRandomValues: (b) => b.fill(7) };
  const id = uuid4(fake);
  assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(uuid4({ randomUUID: () => "fixed" }), "fixed");
  assert.notEqual(uuid4(), uuid4());
});

test("긴 글 줄이기", () => {
  assert.equal(clip("짧은 글"), "짧은 글");
  assert.equal(clip("가나다라마바사", 3), "가나다…");
  assert.equal(clip("줄이\n바뀌는   글", 30), "줄이 바뀌는 글");
  assert.equal(clip("좋아요 😊😊😊", 5), "좋아요 😊…"); // 이모지를 반으로 자르지 않는다
  assert.equal(clip(null), "");
});

test("시각 표기(+09:00 · Z · +00:00)가 달라도 같은 시각으로 보여 준다", () => {
  // 같은 순간: 미리보기 표본은 +09:00, 저장된 메시지는 Z, 내려받은 파일은 +00:00으로 온다
  const forms = ["2026-10-05T09:25:00+09:00", "2026-10-05T00:25:00Z", "2026-10-05T00:25:00+00:00", "2026-10-05T00:25:00.000000Z"];
  const opts = { offsetMinutes: 540, now: new Date("2026-11-11T00:00:00Z") };
  for (const f of forms) {
    assert.equal(formatDateTime(f, opts), "10월 5일 09:25", f);
    assert.equal(dayKey(f, { offsetMinutes: 540 }), "2026-10-05", f);
    assert.equal(formatTime(f, { offsetMinutes: 540 }), "09:25", f);
  }
  // 한국 시간 자정을 넘는 경우도 날짜가 어긋나지 않는다
  assert.equal(dayKey("2026-10-05T15:30:00Z", { offsetMinutes: 540 }), "2026-10-06");
  assert.equal(formatRange("2026-10-05T00:25:00Z", "2026-10-06T14:20:00+09:00", { offsetMinutes: 540 }), "2026-10-05 ~ 2026-10-06");
});
