// 앱 셸(index.html)과 정적 문서의 약속을 지키는지 파일을 읽어 확인한다.
//  - CSP(script-src 'self')에 걸리는 인라인 스크립트·인라인 핸들러·외부 리소스가 없어야 한다(계약 §5).
//  - modulepreload 목록이 js/ 아래 실제 모듈과 같아야 한다.
//  - 화면 CSS에 색 값을 직접 적지 않는다(tokens.css의 변수만).
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(join(WEB, name), "utf8");

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(relative(WEB, path).split("\\").join("/"));
  }
  return out;
}

// 관리자 화면(js/admin.js, js/admin/**)은 다른 담당의 파일이라 여기서 보지 않는다
const isMine = (path) => !/^js\/admin(\.js$|\/)/.test(path);
const PAGES = ["index.html", "privacy.html", "terms.html"];

test("index.html의 modulepreload 목록이 js/ 아래 모듈과 같다", () => {
  const html = read("index.html");
  const listed = [...html.matchAll(/<link rel="modulepreload" href="\/static\/([^"]+)"/g)].map((m) => m[1]).sort();
  const actual = walk(join(WEB, "js"))
    .filter((p) => p.endsWith(".js") && isMine(p) && p !== "js/main.js" && p !== "js/theme-boot.js")
    .sort();
  assert.deepEqual(listed, actual);
});

for (const page of PAGES) {
  test(`${page}: 인라인 스크립트·인라인 핸들러·style 속성·외부 리소스가 없다`, () => {
    const html = read(page);
    for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
      assert.match(m[1], /\bsrc="\/static\//, "스크립트는 같은 출처의 파일로만");
      assert.equal(m[2].trim(), "", "인라인 스크립트 금지");
    }
    assert.doesNotMatch(html, /\son[a-z]+\s*=\s*["']/i, "onclick 같은 인라인 핸들러 금지");
    assert.doesNotMatch(html, /\sstyle\s*=\s*["']/i, "style 속성 대신 클래스를 쓴다");
    for (const m of html.matchAll(/(?:src|href)\s*=\s*"([^"]+)"/g)) {
      assert.doesNotMatch(m[1], /^(https?:)?\/\//, `외부 리소스 금지: ${m[1]}`);
    }
    assert.match(html, /<html lang="ko">/);
    assert.match(html, /href="\/static\/css\/tokens\.css"[\s\S]*href="\/static\/css\/app\.css"/, "tokens.css를 먼저 불러온다");
  });
}

test("안내 문서에는 '초안 — 공개 전 검토 필요' 표시가 있다", () => {
  for (const page of ["privacy.html", "terms.html"]) {
    assert.match(read(page), /초안 — 공개 전 검토 필요/);
  }
});

test("화면 CSS는 색 값을 직접 적지 않고 tokens.css의 변수만 쓴다", () => {
  const tokens = read("css/tokens.css");
  const mine = walk(join(WEB, "css")).filter((p) => /^css\/app[^/]*\.css$/.test(p));
  assert.ok(mine.length >= 1);
  for (const file of mine) {
    const css = read(file).replace(/\/\*[\s\S]*?\*\//g, "");
    assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/, `${file}: 색 값 직접 사용`);
    assert.doesNotMatch(css, /url\(|@import/, `${file}: 외부 리소스`);
    for (const m of css.matchAll(/var\((--[\w-]+)/g)) {
      if (m[1] === "--w") continue; // 스켈레톤 줄 너비(스크립트가 넣는 값)
      assert.ok(tokens.includes(`${m[1]}:`), `${file}: tokens.css에 없는 변수 ${m[1]}`);
    }
  }
});

test("화면 스크립트는 innerHTML·eval·브라우저 기본 대화상자를 쓰지 않는다", () => {
  const files = walk(join(WEB, "js")).filter((p) => p.endsWith(".js") && isMine(p));
  for (const file of files) {
    const raw = read(file);
    const src = raw.replace(/^\s*\/\/[^\n]*$|\/\*[\s\S]*?\*\//gm, ""); // 주석 줄 제외
    assert.doesNotMatch(src, /\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write\(|\beval\(|new Function\(/, `${file}: 문자열을 HTML로 넣지 않는다`);
    assert.doesNotMatch(src, /(?<![\w$])(?:alert|prompt|confirm)\(/, `${file}: alert/prompt/confirm 금지`);
    assert.doesNotMatch(raw, /["'`](?:https?:)?\/\/[a-z0-9.-]+\.[a-z]{2,}/i, `${file}: 외부 주소`);
  }
});
