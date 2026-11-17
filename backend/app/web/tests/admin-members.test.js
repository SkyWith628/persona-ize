// 관리자 화면 '회원 관리' 검사: 표시·요청 규칙(순수 함수)과 표 그리기. 브라우저 없이, 그리기에 쓰는 만큼만 흉내 낸 문서로 돌린다.
import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, requestInit } from "../js/admin/api.js";
import {
  ACTION_LABEL,
  label,
  lastPageOffset,
  memberActionDoneText,
  memberActionErrorText,
  memberActionPlan,
  memberActions,
  memberFilterText,
  memberLockNote,
  memberRoleText,
  memberStatusView,
  membersJumpText,
  membersPageText,
  membersQuery,
  providersText,
} from "../js/admin/format.js";

class FakeNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.className = "";
    this.textContent = "";
    this.attrs = {};
    this.children = [];
    this.listeners = {};
  }
  setAttribute(name, value) {
    this.attrs[name] = value;
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  addEventListener(type, handler) {
    (this.listeners[type] ??= []).push(handler);
  }
  /** 이 노드 아래의 모든 요소 (문자열 자식은 뺀다). */
  all() {
    const nodes = this.children.filter((c) => c instanceof FakeNode);
    return nodes.flatMap((n) => [n, ...n.all()]);
  }
  /** 화면에 보일 글자 전부. */
  text() {
    return this.textContent + this.children.map((c) => (c instanceof FakeNode ? c.text() : String(c))).join("");
  }
}

globalThis.document = { createElement: (tagName) => new FakeNode(tagName) };
const { renderMembers } = await import("../js/admin/view.js");

const ME = "11111111-1111-1111-1111-111111111111";

function member(patch = {}) {
  return {
    user_id: "22222222-2222-2222-2222-222222222222",
    username: "hong",
    email: "hong@example.com",
    email_verified: false,
    display_name: "홍길동",
    role: "user",
    status: "active",
    is_admin: false,
    config_admin: false,
    providers: ["password"],
    created_at: "2026-11-10T01:00:00+00:00",
    last_login_at: null,
    consent_version: "2026-11-01",
    ...patch,
  };
}

const keys = (actions) => actions.map((a) => a.key);
const blocked = (actions) => Object.fromEntries(actions.filter((a) => a.blocked).map((a) => [a.key, a.blocked]));

test("목록 요청: 빈 검색어·필터는 싣지 않고, 검색어는 다듬어 100자로 자른다", () => {
  assert.equal(membersQuery(), "limit=50&offset=0");
  assert.equal(
    membersQuery({ query: "  hong@example.com ", status: "pending", role: "admin", limit: 50, offset: 100 }),
    "query=hong%40example.com&status=pending&role=admin&limit=50&offset=100",
  );
  assert.equal(membersQuery({ query: "   ", status: "", role: "", offset: -5 }), "limit=50&offset=0");
  const long = new URLSearchParams(membersQuery({ query: "a".repeat(150) }));
  assert.equal(long.get("query").length, 100);
});

test("상태·권한·로그인 수단 문구: 모르는 값은 지어내지 않는다", () => {
  assert.deepEqual(memberStatusView("active"), { kind: "ok", text: "사용 중" });
  assert.deepEqual(memberStatusView("pending"), { kind: "warn", text: "승인 대기" });
  assert.deepEqual(memberStatusView("suspended"), { kind: "bad", text: "정지" });
  assert.deepEqual(memberStatusView("frozen"), { kind: "off", text: "frozen" });
  assert.deepEqual(memberStatusView(null), { kind: "off", text: "–" });

  assert.equal(memberRoleText(member()), "일반");
  assert.equal(memberRoleText(member({ role: "admin", is_admin: true })), "관리자");
  assert.equal(memberRoleText(member({ role: "user", is_admin: true, config_admin: true })), "관리자 (서버 설정)");

  assert.equal(providersText(["password", "google", "kakao"]), "비밀번호 · Google · 카카오");
  assert.equal(providersText(["naver"]), "naver");
  assert.equal(providersText([]), "–");
  assert.equal(providersText(undefined), "–");
});

test("상태 필터 버튼·바로 가기·쪽 안내", () => {
  const counts = { active: 1234, pending: 3, suspended: 0 };
  assert.equal(memberFilterText("", counts), "전체");
  assert.equal(memberFilterText("active", counts), "사용 중 1,234");
  assert.equal(memberFilterText("pending", counts), "승인 대기 3");
  assert.equal(memberFilterText("suspended", counts), "정지 0");
  assert.equal(memberFilterText("pending", null), "승인 대기"); // 아직 수를 모른다

  assert.equal(membersJumpText(counts), "회원 관리 · 승인 대기 3명");
  assert.equal(membersJumpText({ active: 5, pending: 0, suspended: 1 }), "회원 관리");
  assert.equal(membersJumpText(undefined), "회원 관리");

  assert.equal(membersPageText(0, 50, 132), "1–50 / 전체 회원 132명");
  assert.equal(membersPageText(100, 32, 132, true), "101–132 / 조건에 맞는 회원 132명");
  assert.equal(membersPageText(0, 0, 0), "전체 회원 0명");

  assert.equal(lastPageOffset(0, 50), 0);
  assert.equal(lastPageOffset(50, 50), 0);
  assert.equal(lastPageOffset(51, 50), 50);
  assert.equal(lastPageOffset(150, 50), 100);
});

test("버튼 목록: 상태·권한에 따라 달라지고, 이름은 누구에게 무엇을 하는지 말한다", () => {
  const active = memberActions(member(), ME);
  assert.deepEqual(keys(active), ["suspend", "promote", "revoke", "delete"]);
  assert.deepEqual(blocked(active), {});
  assert.deepEqual(
    active.map((a) => a.ariaLabel),
    ["hong 님 계정 정지", "hong 님 관리자 지정", "hong 님 세션 모두 끊기", "hong 님 회원 삭제"],
  );
  assert.deepEqual(
    active.map((a) => a.label),
    ["정지", "관리자 지정", "세션 끊기", "삭제"],
  );
  assert.deepEqual(
    active.map((a) => [a.primary, a.danger]),
    [
      [false, false],
      [false, false],
      [false, false],
      [false, true],
    ],
  );

  const pending = memberActions(member({ status: "pending" }), ME);
  assert.deepEqual(keys(pending), ["approve", "suspend", "promote", "revoke", "delete"]);
  assert.equal(pending[0].primary, true);
  assert.equal(pending[0].ariaLabel, "hong 님 가입 승인");

  const suspended = memberActions(member({ status: "suspended", role: "admin", is_admin: true }), ME);
  assert.deepEqual(keys(suspended), ["activate", "demote", "revoke", "delete"]);
  assert.deepEqual(blocked(suspended), {});
});

test("서버 규칙을 미리 지킨다: 서버 설정 관리자는 정지·삭제를, 자기 자신은 정지·관리자 해제·삭제를 못 누른다", () => {
  const config = memberActions(member({ role: "admin", is_admin: true, config_admin: true }), ME);
  assert.deepEqual(blocked(config), { suspend: "서버 설정 관리자", delete: "서버 설정 관리자" });
  assert.equal(config[0].ariaLabel, "hong 님 계정 정지 (할 수 없음: 서버 설정 관리자)");
  assert.match(memberLockNote(member({ config_admin: true }), ME), /ADMIN_USERNAMES.*정지·삭제할 수 없습니다/);

  const self = memberActions(member({ user_id: ME, role: "admin", is_admin: true }), ME);
  assert.deepEqual(blocked(self), { suspend: "내 계정", demote: "내 계정", delete: "내 계정" });
  assert.equal(self.find((a) => a.key === "revoke").blocked, null); // 내 세션 끊기는 서버도 허용한다
  assert.match(memberLockNote(member({ user_id: ME }), ME), /^내 계정입니다/);

  // 내가 누구인지 모르면(이름을 못 읽음) 막지 않는다 — 서버가 409로 막는다
  assert.deepEqual(blocked(memberActions(member({ user_id: ME, role: "admin" }), null)), {});
  assert.equal(memberLockNote(member(), ME), null);
});

test("버튼마다 보내는 요청: 경로·메서드·본문", () => {
  const m = member();
  const base = "/v1/admin/members/22222222-2222-2222-2222-222222222222";
  const plan = (key, who = m) => {
    const p = memberActionPlan(key, who, ME);
    return [p.method, p.path, p.body];
  };
  // 상태를 바꾸는 요청은 이 줄을 그릴 때의 상태를 함께 보낸다 (그 사이 바뀌었으면 서버가 409 member_changed)
  const pending = member({ status: "pending" });
  const suspended = member({ status: "suspended" });
  assert.deepEqual(plan("approve", pending), ["PATCH", base, { status: "active", expected_status: "pending" }]);
  assert.deepEqual(plan("activate", suspended), ["PATCH", base, { status: "active", expected_status: "suspended" }]);
  assert.deepEqual(plan("suspend"), ["PATCH", base, { status: "suspended", expected_status: "active" }]);
  assert.deepEqual(plan("suspend", pending), ["PATCH", base, { status: "suspended", expected_status: "pending" }]);
  assert.deepEqual(plan("promote"), ["PATCH", base, { role: "admin" }]);
  assert.deepEqual(plan("demote"), ["PATCH", base, { role: "user" }]);
  assert.deepEqual(plan("revoke"), ["POST", `${base}/revoke-sessions`, undefined]);
  assert.deepEqual(plan("delete"), ["DELETE", base, undefined]);
  assert.throws(() => memberActionPlan("explode", m, ME), /unknown member action/);
  // 경로에 들어가는 id는 인코딩한다 (서버가 준 값이지만 그대로 믿지 않는다)
  assert.equal(memberActionPlan("delete", member({ user_id: "a/b?c" }), ME).path, "/v1/admin/members/a%2Fb%3Fc");
});

test("확인 대화상자: 모든 일에 확인을 받고, 삭제는 아이디를 입력해야 하며 무엇이 지워지는지 말한다", () => {
  const m = member();
  for (const key of ["approve", "activate", "suspend", "promote", "demote", "revoke", "delete"]) {
    const { confirm } = memberActionPlan(key, m, ME);
    assert.ok(confirm.title.endsWith("까요?"), key);
    assert.ok(confirm.message.length > 0 && confirm.confirmLabel.length > 0, key);
  }
  const del = memberActionPlan("delete", m, ME).confirm;
  assert.equal(del.requireText, "hong");
  assert.equal(del.danger, true);
  assert.match(del.message, /hong 님.*되돌릴 수 없고.*복구할 방법이 없습니다/);
  const erased = del.bullets.join(" / ");
  assert.match(erased, /상대/);
  assert.match(erased, /대화/);
  assert.match(erased, /그래프/);
  assert.match(del.requireLabel, /hong/);

  assert.equal(memberActionPlan("approve", m, ME).confirm.requireText, undefined);
  assert.match(memberActionPlan("suspend", m, ME).confirm.message, /모두 로그아웃/);
  // 내 세션을 끊으면 이 화면도 로그아웃된다는 것을 미리 알린다
  assert.match(memberActionPlan("revoke", member({ user_id: ME }), ME).confirm.message, /이 화면을 포함해/);
  assert.doesNotMatch(memberActionPlan("revoke", m, ME).confirm.message, /이 화면을 포함해/);
  // 서버 설정 관리자의 권한을 바꿔도 관리자로 남는다는 것을 알린다
  assert.match(memberActionPlan("demote", member({ role: "admin", config_admin: true }), ME).confirm.message, /관리자로 남습니다/);
});

test("결과 안내", () => {
  const m = member();
  assert.equal(memberActionDoneText("approve", m, m), "hong 님의 가입을 승인했습니다.");
  assert.equal(memberActionDoneText("revoke", m, { revoked_sessions: 3 }), "hong 님의 세션 3개를 끊었습니다.");
  assert.equal(memberActionDoneText("revoke", m, null), "hong 님의 세션 0개를 끊었습니다.");
  assert.equal(
    memberActionDoneText("delete", m, { contacts: 2, conversations: 3, messages: 1200, graph_nodes_removed: 0, graph_error: null }),
    "hong 님을 삭제했습니다 (상대 2명 · 대화방 3개 · 메시지 1,200건).",
  );
  assert.match(
    memberActionDoneText("delete", m, { contacts: 0, conversations: 0, messages: 0, graph_error: "ServiceUnavailable" }),
    /관계 그래프는 지우지 못했습니다\(ServiceUnavailable\)/,
  );
});

test("오류 안내: 서버 규칙(409)·없는 회원(404)·권한(403)을 구분한다", () => {
  const self = memberActionErrorText("suspend", new ApiError(409, "cannot_modify_self", "x"));
  assert.match(self, /^자기 자신은/);
  const protectedAdmin = memberActionErrorText("delete", new ApiError(409, "protected_admin", "x"));
  assert.match(protectedAdmin, /ADMIN_USERNAMES.*정지하거나 삭제할 수 없습니다/);
  assert.match(memberActionErrorText("delete", new ApiError(404, "not_found", "x")), /이미 삭제됐을 수 있습니다/);
  // 오래된 화면에서 누른 승인·정지: 바꾸지 않았다는 것과 목록을 새로 읽었다는 것을 알린다
  const changed = memberActionErrorText("approve", new ApiError(409, "member_changed", "x"));
  assert.match(changed, /다른 관리자가.*상태를 바꿨습니다.*아무것도 바꾸지 않았습니다.*목록을 새로 읽었으니/);
  const lastAdmin = memberActionErrorText("demote", new ApiError(409, "last_admin", "x"));
  assert.match(lastAdmin, /^마지막 관리자는.*먼저 다른 관리자를 지정하세요/);
  assert.match(memberActionErrorText("promote", new ApiError(403, "forbidden", "x")), /^관리자 지정에 실패했습니다\. 권한이 없거나/);
  assert.equal(
    memberActionErrorText("revoke", new ApiError(0, "network", "서버에 연결하지 못했습니다.")),
    "세션 모두 끊기에 실패했습니다. 서버에 연결하지 못했습니다.",
  );
  // 다른 409(코드가 다르다)는 서버 문장을 그대로 보여 준다
  assert.equal(
    memberActionErrorText("suspend", new ApiError(409, "conflict", "다른 곳에서 먼저 바뀌었습니다.")),
    "계정 정지에 실패했습니다. 다른 곳에서 먼저 바뀌었습니다.",
  );
});

test("접근 기록에 회원 관리 이름이 있다", () => {
  for (const action of [
    "members",
    "member_approve",
    "member_suspend",
    "member_activate",
    "member_role",
    "member_revoke_sessions",
    "member_delete",
    "member_delete_done",
    // 결과 줄(_done·_failed)과 서버 규칙에 걸린 시도
    "member_approve_done",
    "member_approve_failed",
    "member_suspend_done",
    "member_suspend_failed",
    "member_activate_done",
    "member_activate_failed",
    "member_role_done",
    "member_role_failed",
    "member_revoke_sessions_done",
    "member_revoke_sessions_failed",
    "member_delete_failed",
    "member_refused",
  ]) {
    assert.notEqual(label(ACTION_LABEL, action), action, action);
  }
});

test("쓰기 요청: 본문이 있으면 JSON으로 싣고 CSRF 토큰은 헤더에", () => {
  const patch = requestInit("PATCH", "tok", { status: "active" });
  assert.deepEqual(patch.headers, { Accept: "application/json", "X-CSRF-Token": "tok", "Content-Type": "application/json" });
  assert.equal(patch.body, '{"status":"active"}');
  const del = requestInit("DELETE", "tok");
  assert.deepEqual(del.headers, { Accept: "application/json", "X-CSRF-Token": "tok" });
  assert.equal("body" in del, false);
  assert.equal("body" in requestInit("GET", "tok", { a: 1 }), false); // 읽기에는 본문도 토큰도 없다
});

// ---------- 표 그리기 ----------

function draw(items, opts = {}, total = items.length) {
  const root = new FakeNode("div");
  renderMembers(root, { total, counts: { active: 0, pending: 0, suspended: 0 }, items }, opts);
  return { root, buttons: root.all().filter((n) => n.tagName === "button") };
}

test("회원 표: 한 줄에 이름·이메일·상태·로그인 수단과 버튼이 있고, 누르면 (종류, 회원, 버튼)을 넘긴다", () => {
  const seen = [];
  const m = member({ status: "pending", providers: ["google"], email_verified: true });
  const { root, buttons } = draw([m], { selfId: ME, onAction: (...args) => seen.push(args) });
  const text = root.text();
  assert.match(text, /hong/);
  assert.match(text, /홍길동/);
  assert.match(text, /hong@example\.com✓확인됨/);
  assert.match(text, /!승인 대기/);
  assert.match(text, /Google/);
  assert.deepEqual(
    buttons.map((b) => b.textContent),
    ["승인", "정지", "관리자 지정", "세션 끊기", "삭제"],
  );
  for (const b of buttons) {
    assert.equal(b.attrs.type, "button");
    assert.equal(b.attrs["data-user"], m.user_id);
    assert.match(b.attrs["aria-label"], /^hong 님 /);
    assert.equal("disabled" in b.attrs, false);
  }
  buttons[0].listeners.click[0]();
  assert.deepEqual(seen, [["approve", m, buttons[0]]]);
});

test("회원 표: 막힌 버튼은 비활성이고 누를 수 없으며 까닭이 글로 보인다", () => {
  const onAction = () => assert.fail("부르면 안 된다");
  const { root, buttons } = draw([member({ role: "admin", is_admin: true, config_admin: true })], { selfId: ME, onAction });
  const off = buttons.filter((b) => "disabled" in b.attrs);
  assert.deepEqual(
    off.map((b) => b.attrs["data-action"]),
    ["suspend", "delete"],
  );
  for (const b of off) assert.equal(b.listeners.click, undefined);
  assert.match(root.text(), /서버 설정\(ADMIN_USERNAMES\)으로 지정된 관리자입니다/);
  assert.match(root.text(), /관리자 \(서버 설정\)/);

  const mine = draw([member({ user_id: ME, role: "admin", is_admin: true })], { selfId: ME, onAction });
  assert.deepEqual(
    mine.buttons.filter((b) => "disabled" in b.attrs).map((b) => b.attrs["data-action"]),
    ["suspend", "demote", "delete"],
  );
  assert.match(mine.root.text(), /내 계정입니다/);
});

test("회원 표: 이메일이 없거나 확인되지 않은 회원, 빈 목록", () => {
  const { root } = draw([member({ email: null, display_name: null, providers: [] })], { selfId: ME });
  assert.doesNotMatch(root.text(), /확인됨|미확인/);
  assert.match(draw([member()], { selfId: ME }).root.text(), /hong@example\.com미확인/);

  assert.match(draw([], {}, 0).root.text(), /가입한 회원이 없습니다/);
  assert.match(draw([], { filtered: true }, 0).root.text(), /조건에 맞는 회원이 없습니다/);
});
