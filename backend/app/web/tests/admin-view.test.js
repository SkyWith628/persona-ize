// 관리자 수집 표의 "다시 시도" 버튼 검사. 브라우저 없이, 그리기에 쓰는 만큼만 흉내 낸 문서로 돌린다.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
const { renderHeld, renderIngest, renderJobs } = await import("../js/admin/view.js");

function overview(graph) {
  return {
    graph: {
      enabled: true,
      pending_messages: 0,
      held_messages: 0,
      jobs: { pending: 0, running: 0, failed: 0, limited: 0 },
      ...graph,
    },
    health: { postgres: "ok", neo4j: "ok" },
  };
}

function draw(graph, actions) {
  const root = new FakeNode("div");
  renderIngest(root, overview(graph), actions);
  return { root, buttons: root.all().filter((n) => n.tagName === "button") };
}

test("보류가 있으면 보류 줄에 다시 시도 버튼이 있고, 누르면 건수와 버튼을 넘긴다", () => {
  const seen = [];
  const { root, buttons } = draw({ held_messages: 4 }, { onRetryHeld: (count, button) => seen.push([count, button]) });
  assert.equal(buttons.length, 1);
  const [button] = buttons;
  assert.deepEqual([button.textContent, button.attrs.type], ["다시 시도", "button"]);
  // 버튼은 보류 줄 안에 있다
  const row = root.all().find((n) => n.className === "row" && n.all().includes(button));
  assert.match(row.text(), /거듭 실패해 보류된 메시지4건/);
  assert.doesNotMatch(root.text(), /graph_reset/);

  button.listeners.click[0]();
  assert.deepEqual(seen, [[4, button]]);
});

test("보류가 0건이거나 수집이 꺼져 있거나 처리기가 없으면 버튼이 없다", () => {
  const onRetryHeld = () => assert.fail("부르면 안 된다");
  const none = draw({ held_messages: 0 }, { onRetryHeld });
  assert.equal(none.buttons.length, 0);
  assert.match(none.root.text(), /거듭 실패해 보류된 메시지0건 ✓없음/);

  const off = draw({ enabled: false, held_messages: 4 }, { onRetryHeld });
  assert.equal(off.buttons.length, 0);
  assert.match(off.root.text(), /4건 –수집이 꺼져 다시 시도할 수 없음/);

  assert.equal(draw({ held_messages: 4 }).buttons.length, 0); // 처리기를 주지 않은 호출(읽기 전용)
});

// ---------- 보류 표 (대화방별) ----------

const GROUPS = [
  {
    user_id: "u_8f2…a1",
    conversation_id: "11111111-2222-4333-8444-555555555555",
    messages: 2,
    attempts: 3,
    oldest_held_at: "2026-11-20T03:00:00Z",
    latest_held_at: "2026-11-20T03:01:00Z",
  },
  {
    user_id: "u_c07…9e",
    conversation_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    messages: 1,
    attempts: 3,
    oldest_held_at: "2026-11-19T01:00:00Z",
    latest_held_at: "2026-11-19T01:00:00Z",
  },
];

function drawHeld(items, actions) {
  const root = new FakeNode("div");
  renderHeld(root, items, actions);
  return { root, buttons: root.all().filter((n) => n.tagName === "button") };
}

test("보류 표: 줄마다 다시 시도 버튼이 있고, 누르면 그 줄과 버튼을 넘긴다", () => {
  const seen = [];
  const { root, buttons } = drawHeld(GROUPS, { canRetry: true, onRetry: (group, button) => seen.push([group, button]) });
  assert.equal(buttons.length, 2);
  assert.deepEqual(
    buttons.map((b) => [b.textContent, b.attrs.type, b.attrs["data-conversation"]]),
    GROUPS.map((g) => ["다시 시도", "button", g.conversation_id]),
  );
  const rows = root.all().filter((n) => n.tagName === "tr");
  assert.equal(rows.length, 3); // 머리 1 + 줄 2
  assert.match(rows[1].text(), /u_8f2…a1.*11111111….*2건.*3번/);
  // 대화방 id 전체는 화면 글자로 나오지 않는다 (요청에만 싣는다)
  assert.doesNotMatch(root.text(), /11111111-2222/);

  buttons[1].listeners.click[0]();
  assert.deepEqual(seen, [[GROUPS[1], buttons[1]]]);
});

test("보류 표: 수집이 꺼져 있거나 처리기가 없으면 버튼이 없고, 비면 안내만 낸다", () => {
  const onRetry = () => assert.fail("부르면 안 된다");
  const off = drawHeld(GROUPS, { canRetry: false, onRetry });
  assert.equal(off.buttons.length, 0);
  assert.match(off.root.text(), /수집이 꺼져 있어 다시 시도할 수 없습니다/);
  assert.equal(drawHeld(GROUPS, { canRetry: true }).buttons.length, 0);
  assert.equal(drawHeld(GROUPS).buttons.length, 0);

  const none = drawHeld([], { canRetry: true, onRetry });
  assert.equal(none.buttons.length, 0);
  assert.equal(none.root.text(), "보류된 메시지가 없습니다.");

  // 건수가 0이거나 대화방 id가 없는 줄에는 버튼을 내지 않는다
  const odd = drawHeld([{ ...GROUPS[0], messages: 0 }, { ...GROUPS[1], conversation_id: null }], { canRetry: true, onRetry });
  assert.equal(odd.buttons.length, 0);
});

test("작업 표: 역할 고치기는 종류로 가르고 메시지 수를 적지 않는다 (전부 중복인 다시 올리기는 중복 건수로 가른다)", () => {
  const job = (over) => ({
    id: "j",
    user_id: "u_8f2…a1",
    status: "completed",
    kind: "import",
    imported: 3,
    duplicates: 0,
    episodes: 2,
    error_code: null,
    graph_status: "skipped",
    graph_episodes: 0,
    graph_error: null,
    created_at: "2026-11-25T01:00:00Z",
    ...over,
  });
  const root = new FakeNode("div");
  renderJobs(root, [
    job({}),
    job({ kind: "speaker_roles", imported: 0 }),
    job({ imported: 0, duplicates: 12 }),
    job({ imported: 8, duplicates: 4 }),
    job({ imported: 0 }),
  ]);
  const rows = root.all().filter((n) => n.tagName === "tr");
  const cells = (row) => row.children.map((td) => td.text());
  const [, , kind, , messages] = cells(rows[0]);
  assert.deepEqual([kind, messages], ["종류", "메시지"]);
  assert.deepEqual([cells(rows[1])[2], cells(rows[1])[4]], ["가져오기", "3건"]);
  assert.deepEqual([cells(rows[2])[2], cells(rows[2])[4]], ["역할 고치기", "–"]);
  assert.deepEqual([cells(rows[3])[2], cells(rows[3])[4]], ["가져오기", "0건 (중복 12건)"]);
  assert.deepEqual([cells(rows[4])[2], cells(rows[4])[4]], ["가져오기", "8건 (중복 4건)"]);
  assert.deepEqual([cells(rows[5])[2], cells(rows[5])[4]], ["가져오기", "0건"]);
  // 숫자 칸(오른쪽 정렬)이고 안내 문구가 "중복"을 풀어 준다
  assert.ok(rows[3].children[4].className.includes("num"));
  assert.ok(root.text().includes("이미 있어 넣지 않은 건수"));
});

// ---------- 브라우저에서만 드러난 흠 (2026-11-25, 폭 390·768px) ----------
// 그리기 흉내 문서로는 볼 수 없어 원본을 읽어 확인한다.

const source = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("표 스크롤 영역: 가려진 쪽에 그림자로 더 있음을 알린다 (색은 토큰만)", () => {
  const css = source("../css/admin.css");
  const rule = /\n\.table-wrap \{([^}]*)\}/.exec(css)?.[1] ?? "";
  assert.match(rule, /overflow-x:\s*auto/);
  // 가림막 두 겹은 내용과 함께 움직이고(local) 그림자 두 겹은 제자리에 있다(scroll)
  assert.match(rule, /background-attachment:\s*local,\s*local,\s*scroll,\s*scroll/);
  assert.equal((rule.match(/var\(--surface\)/g) ?? []).length, 2);
  assert.doesNotMatch(rule, /#[0-9a-f]{3,8}\b|rgba?\(/i);
});

test("다시 시도: 실패하면 버튼에 포커스를 돌려주고, 새로 고치면 지난 안내를 지운다", () => {
  const js = source("../js/admin.js");
  const retry = js.slice(js.indexOf("async function retryHeld"), js.indexOf("function renderUsersPager"));
  const failed = retry.slice(retry.indexOf("} catch (error) {"), retry.indexOf("return;", retry.indexOf("} catch (error) {")));
  assert.match(failed, /button\.disabled = false;[\s\S]*button\.focus\(\)/);
  const loadAll = js.slice(js.indexOf("async function loadAll"), js.indexOf("async function changeDays"));
  assert.ok(loadAll.indexOf('setHeldNote("")') > 0);
  assert.ok(loadAll.indexOf('setHeldNote("")') < loadAll.indexOf("await loadOverview()"));
});
