// 관리자 수집 표의 "다시 시도" 버튼 검사. 브라우저 없이, 그리기에 쓰는 만큼만 흉내 낸 문서로 돌린다.
import assert from "node:assert/strict";
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
const { renderIngest } = await import("../js/admin/view.js");

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
