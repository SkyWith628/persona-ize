import assert from "node:assert/strict";
import { test } from "node:test";

import { buildHash, matchRoute, parseHash } from "../js/lib/router.js";

test("해시를 경로와 쿼리로 나눈다", () => {
  assert.deepEqual(parseHash("#/contacts/abc?tab=persona&x="), {
    path: "/contacts/abc",
    segments: ["contacts", "abc"],
    query: { tab: "persona", x: "" },
  });
  assert.deepEqual(parseHash(""), { path: "/", segments: [], query: {} });
  assert.deepEqual(parseHash("#"), { path: "/", segments: [], query: {} });
});

test("화면 이름과 주소 값을 찾는다", () => {
  assert.equal(matchRoute("#/compose").name, "compose");
  const c = matchRoute("#/contacts/3f2a?from=compose");
  assert.equal(c.name, "contact");
  assert.equal(c.nav, "contacts");
  assert.deepEqual(c.params, { id: "3f2a" });
  assert.deepEqual(c.query, { from: "compose" });
  assert.equal(matchRoute("#/import?contact=1").name, "import");
});

test("없는 주소는 null", () => {
  assert.equal(matchRoute("#/nope"), null);
  assert.equal(matchRoute("#/contacts/a/b"), null);
  assert.equal(matchRoute(""), null);
});

test("주소를 만든다", () => {
  assert.equal(buildHash("compose"), "#/compose");
  assert.equal(buildHash("contact", { id: "a b" }), "#/contacts/a%20b");
  assert.equal(buildHash("history", {}, { contact: "c1", empty: "", none: null }), "#/history?contact=c1");
  assert.throws(() => buildHash("contact"), /주소 값/);
  assert.throws(() => buildHash("unknown"), /없는 화면/);
});

test("만든 주소를 다시 읽으면 같은 값이 나온다", () => {
  const hash = buildHash("contact", { id: "가 나/다" }, { q: "a&b=c" });
  const back = matchRoute(hash);
  assert.equal(back.params.id, "가 나/다");
  assert.equal(back.query.q, "a&b=c");
});

test("깨진 퍼센트 인코딩에도 죽지 않는다", () => {
  assert.equal(matchRoute("#/contacts/%E0%A4%A").name, "contact");
});
