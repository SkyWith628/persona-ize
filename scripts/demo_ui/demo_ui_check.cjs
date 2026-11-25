#!/usr/bin/env node
// 시연 흐름 화면 검사 (선택 도구) — PC 웹(`/#/compose` 등)에서 시연 장면을 헤드리스 크롬으로 눌러 본다.
//
//   node scripts/demo_ui/demo_ui_check.cjs --base-url http://127.0.0.1:8141 --out /tmp/demo-shots
//
// **Playwright와 Chromium이 이미 있는 환경에서만 돈다.** 프로젝트 의존성이 아니다(package.json도 없다) —
// `require("playwright")`가 안 되면 설치하지 말고 API 검사(`scripts/demo_check.py`, `make demo-check-docker`)
// 만 쓴다. CI에서는 돌지 않는다. 서버에 요청 제한(RATE_LIMIT_PER_MINUTE)이 걸려 있으면 화면이 보내는
// 요청 수 때문에 429로 깨질 수 있다 — 제한이 없는 개발 서버에 돌린다.
//
// 합성 대화는 `scripts/demo_check.py --dump-data`가 쓴 것을 읽는다(자료가 한 곳에만 있게). --data 를 주지
// 않으면 이 스크립트가 파이썬을 불러 임시 폴더에 만든다.
//
// 장면: 0 가입 → 로그아웃 → 로그인 / 준비 상대 추가 → 카카오톡 .csv 올리기 → 화자 지정 → 가져오기(상대 4명)
//       1 상사 B(지킨 항목·복사) → C(참고한 과거 표현) / 2 동료 C → D / 3 영어 상대 자동
//       4 말투가 변한 지인의 제안 카드 → 적용 → 저장 / 5 이력 → 내 데이터 → 내려받기 / 끝 계정 지우기
// 가짜 모델 서버용이다. 보는 것은 화면이 다음 단계로 넘어가는지뿐이고, 문장 품질·주의 문구는 보지 못한다.

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const LIMITS =
  "보지 못한 것: 문장이 실제로 다듬어지는지, 주의 문구, 원문 유지 화면, 조건 C와 D의 차이(가짜 모델), " +
  "모바일 화면(/m)·아이폰 앱, 화면이 보기 좋은지. 품질은 실제 모델 리허설로 확인한다.";
const REL_LABEL = { superior: "상사", colleague: "동료·동기", acquaintance: "지인·거래처" };
const ROLE_LABEL = { me: "나", contact: "상대", skip: "제외" };

function parseArgs(argv) {
  const args = { baseUrl: "http://127.0.0.1:8141", out: null, data: null, invite: "", width: 1280, timeout: 10000 };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    const value = () => {
      i += 1;
      if (argv[i] === undefined) throw new Error(`${flag} 에 값이 필요합니다`);
      return argv[i];
    };
    if (flag === "--base-url") args.baseUrl = value().replace(/\/$/, "");
    else if (flag === "--out") args.out = value();
    else if (flag === "--data") args.data = value();
    else if (flag === "--invite-code") args.invite = value();
    else if (flag === "--width") args.width = Number(value());
    else if (flag === "--timeout") args.timeout = Number(value()) * 1000;
    else if (flag === "--assume-fake") args.assumeFake = true;
    else if (flag === "--allow-real-model") args.allowReal = true;
    else if (flag === "--keep-account") args.keep = true;
    else if (flag === "--headed") args.headed = true;
    else if (flag === "--help" || flag === "-h") args.help = true;
    else throw new Error(`모르는 인자: ${flag}`);
  }
  return args;
}

function loadData(dir) {
  let folder = dir;
  if (!folder) {
    folder = fs.mkdtempSync(path.join(os.tmpdir(), "pz-demo-data-"));
    const script = path.join(__dirname, "..", "demo_check.py");
    let made = false;
    for (const python of [process.env.PYTHON, "python3", "python"].filter(Boolean)) {
      try {
        execFileSync(python, [script, "--dump-data", folder], { stdio: "ignore" });
        made = true;
        break;
      } catch {
        // 다음 후보
      }
    }
    if (!made) throw new Error("파이썬으로 합성 대화를 만들지 못했습니다. demo_check.py --dump-data DIR 뒤 --data DIR");
  }
  const manifest = JSON.parse(fs.readFileSync(path.join(folder, "manifest.json"), "utf-8"));
  for (const c of manifest.contacts) c.path = path.join(folder, c.file);
  return manifest;
}

class StepError extends Error {}
class Skipped extends Error {}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log("node scripts/demo_ui/demo_ui_check.cjs [--base-url URL] [--out DIR] [--data DIR] [--invite-code 코드]");
    console.log("    [--width 1280] [--timeout 초] [--assume-fake | --allow-real-model] [--keep-account] [--headed]");
    return 0;
  }
  let chromium;
  try {
    ({ chromium } = require("playwright"));
  } catch {
    console.log("playwright를 찾지 못했습니다 — 이 도구는 Playwright가 이미 있는 환경에서만 씁니다(설치하지 않는다).");
    console.log("화면 없이 경로만 보려면: python3 scripts/demo_check.py --base-url … 또는 make demo-check-docker");
    return 2;
  }
  const data = loadData(args.data);
  const byKey = Object.fromEntries(data.contacts.map((c) => [c.key, c]));
  if (args.out) fs.mkdirSync(args.out, { recursive: true });

  // 가짜 모델 서버인지 먼저 본다 (실제 모델이면 첨삭과 그래프 수집이 실제 호출로 나간다)
  const health = await fetch(`${args.baseUrl}/healthz`).then((r) => r.json()).catch(() => null);
  if (!health) {
    console.log(`[실패] 장면 0 · 헬스체크 — ${args.baseUrl} 에 닿지 못했습니다`);
    return 1;
  }
  if (health.llm_provider !== "fake" && !args.assumeFake && !args.allowReal) {
    console.log(
      `[실패] 장면 0 · 헬스체크 — 서버의 모델이 '${health.llm_provider ?? "알 수 없음(운영 모드)"}'입니다. ` +
        "가짜 모델 서버면 --assume-fake, 비용을 알고 돌리려면 --allow-real-model",
    );
    return 1;
  }

  const browser = await chromium.launch(args.headed ? { headless: false } : undefined);
  // 시연 PC와 같은 시간대·언어로 (헤드리스 기본은 UTC라 대화 시각이 9시간 어긋나 보인다)
  const context = await browser.newContext({
    viewport: { width: args.width, height: 900 },
    acceptDownloads: true,
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
  });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(args.baseUrl).origin }).catch(() => {});
  const page = await context.newPage();
  page.setDefaultTimeout(args.timeout);

  // 화면 오류: 스크립트 예외와 5xx 응답은 그 단계의 실패로 친다. 401(로그인 전 /me)은 정상이다.
  const problems = [];
  page.on("pageerror", (err) => problems.push(`화면 스크립트 오류 ${err.name}`));
  page.on("response", (res) => {
    if (res.status() === 429) problems.push("요청 제한(429) — 요청 제한이 없는 서버에 돌리세요");
    if (res.status() >= 500) problems.push(`HTTP ${res.status()} ${res.request().method()} ${new URL(res.url()).pathname}`);
  });

  const report = { passed: 0, failed: [], skipped: [], shots: [] };
  const started = Date.now();
  const user = { name: `demoui-${Math.random().toString(36).slice(2, 10)}`, password: `Ui-${Math.random().toString(36).slice(2)}-${Date.now()}` };
  const state = { registered: false, deleted: false, ready: new Set(), rewrites: 0 };
  let scene = "0";
  let shotNo = 0;

  const shot = async (name) => {
    if (!args.out) return;
    shotNo += 1;
    const file = path.join(args.out, `${String(shotNo).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true }).catch(() => {});
    report.shots.push(file);
  };
  const check = (cond, message) => {
    if (!cond) throw new StepError(message);
  };
  // 라디오 단추는 입력을 숨기고 라벨을 단추처럼 그린다 — 사람처럼 라벨을 누른다
  const choose = async (radio) => {
    if (await radio.isChecked()) return;
    const id = await radio.getAttribute("id");
    await page.locator(`label[for="${id}"]`).click();
    check(await radio.isChecked(), "라디오 단추가 골라지지 않았습니다");
  };
  const go = async (hash) => {
    await page.goto(`${args.baseUrl}/#/${hash}`);
  };
  const step = async (name, fn, capture) => {
    const label = `장면 ${scene} · ${name}`;
    const t0 = Date.now();
    problems.length = 0;
    try {
      const detail = (await fn()) || "";
      check(problems.length === 0, problems.join(" / "));
      report.passed += 1;
      console.log(`[ OK ] ${label}${detail ? ` — ${detail}` : ""}  (${Date.now() - t0}ms)`);
      if (capture) await shot(capture);
      return true;
    } catch (err) {
      if (err instanceof Skipped) {
        report.skipped.push(`${label} — ${err.message}`);
        console.log(`[건너뜀] ${label} — ${err.message}`);
        return false;
      }
      // Playwright 오류 문장에는 화면 글자가 섞일 수 있다 — 첫 줄(무엇을 기다렸는지)만 남긴다
      const why = err instanceof StepError ? err.message : `${err.name}: ${String(err.message).split("\n")[0].slice(0, 160)}`;
      report.failed.push(`${label} — ${why}`);
      console.log(`[실패] ${label} — ${why}  (${Date.now() - t0}ms)`);
      await shot(`fail-${scene}-${name.replace(/[^0-9A-Za-z가-힣]+/g, "_")}`);
      return false;
    }
  };
  const ready = (key) => {
    if (!state.ready.has(key)) throw new Skipped(`준비 단계에서 '${key}' 상대의 대화를 가져오지 못했습니다`);
    return byKey[key];
  };

  // ---- 장면 0 ----
  const fillAuth = async () => {
    await page.getByLabel("아이디", { exact: false }).first().fill(user.name);
    await page.getByLabel("비밀번호", { exact: true }).fill(user.password);
  };
  const signup = async () => {
    await page.goto(`${args.baseUrl}/`);
    await page.getByRole("button", { name: "가입 화면" }).click();
    await page.getByRole("button", { name: "이메일 없이 아이디로 가입하기" }).click();
    await fillAuth();
    await page.getByLabel("비밀번호 확인").fill(user.password);
    const invite = page.getByLabel("초대 코드");
    if (await invite.count()) {
      check(Boolean(args.invite), "초대 코드가 필요한 서버입니다 (--invite-code)");
      await invite.fill(args.invite);
    }
    const consent = page.locator(".auth-form .check-row input[type=checkbox]");
    if (await consent.count()) await consent.check();
    await page.getByRole("button", { name: "가입하고 시작하기" }).click();
    await page.waitForURL(/#\/compose/);
    state.registered = true;
    await page.getByRole("heading", { name: "첨삭", level: 1 }).waitFor();
    return `임시 계정 ${user.name}`;
  };
  const relogin = async () => {
    if (!state.registered) throw new Skipped("가입이 되지 않았습니다");
    await page.getByRole("button", { name: /^계정 메뉴/ }).click();
    await page.getByRole("button", { name: "로그아웃" }).click();
    await page.getByRole("button", { name: "로그인", exact: true }).waitFor();
    await fillAuth();
    await page.getByRole("button", { name: "로그인", exact: true }).click();
    await page.getByRole("heading", { name: "첨삭", level: 1 }).waitFor();
    return "로그아웃 → 로그인 → 첨삭 화면";
  };

  // ---- 준비 ----
  const importOne = async (c, first) => {
    if (!state.registered) throw new Skipped("가입이 되지 않았습니다");
    await go("import");
    await page.getByRole("heading", { name: "대화 가져오기", level: 1 }).waitFor();
    // 방금 가져온 결과가 남아 있으면(4단계) 처음으로 돌아간다
    const again = page.getByRole("button", { name: "다른 대화 가져오기" });
    const add = page.getByRole("button", { name: first ? "상대 추가" : "새 상대 추가", exact: true });
    await again.or(add).first().waitFor();
    if (await again.isVisible()) await again.click();
    await add.click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("이름").fill(c.alias);
    await dialog.getByLabel("관계").selectOption({ label: REL_LABEL[c.relationship_type] });
    await dialog.getByRole("button", { name: "추가", exact: true }).click();
    await dialog.waitFor({ state: "hidden" });
    // 1단계 상대 → 2단계 파일
    await choose(page.getByRole("radio", { name: new RegExp(`^${c.alias} `) }));
    await page.getByRole("button", { name: "다음", exact: true }).click();
    await page.getByLabel("카카오톡에서 내보낸 파일").setInputFiles(c.path);
    await page.locator(".file-info").waitFor();
    await page.getByRole("button", { name: "미리보기", exact: true }).click();
    // 3단계 미리보기: 형식과 화자 지정
    await page.getByRole("heading", { name: /이렇게 읽었어요/ }).waitFor();
    check(await page.getByText("카카오톡 (PC · CSV)").count(), "카카오톡 PC .csv로 읽지 않았습니다");
    for (const [name, role] of Object.entries(c.speakers)) {
      await choose(page.getByRole("radio", { name: `${name}: ${ROLE_LABEL[role]}`, exact: true }));
    }
    await page.getByRole("checkbox", { name: /위 내용을 확인했어요/ }).check();
    await page.getByRole("button", { name: "가져오기", exact: true }).click();
    // 4단계: 말투 분석이 끝날 때까지 (화면이 작업 상태를 다시 읽는다)
    await page.getByRole("heading", { name: "가져왔어요" }).waitFor({ timeout: Math.max(args.timeout, 60000) });
    await page.getByText(/말투 분석 완료/).first().waitFor({ timeout: Math.max(args.timeout, 120000) });
    // 처음에는 저장 건수만 보이고, 대화방 통계를 읽은 뒤 "메시지 N개 중 내 메시지 M개"로 바뀐다
    const want = `메시지 ${c.messages}개 중 내 메시지 ${c.my_messages}개`;
    const line = page.locator(".phase-detail").first();
    await page
      .locator(".phase-detail", { hasText: want })
      .first()
      .waitFor()
      .catch(async () => {
        const seen = (await line.innerText().catch(() => "")).slice(0, 60);
        throw new StepError(`가져온 수가 다릅니다 (기대 "${want}", 화면 "${seen}")`);
      });
    const detail = await line.innerText();
    state.ready.add(c.key);
    if (c.key === "changed") {
      await page.getByText(`${c.alias}에게 쓰는 말투를 읽었어요`).waitFor();
      return `${detail} · 말투 제안이 결과 화면에 뜸`;
    }
    return detail;
  };

  // ---- 첨삭 ----
  const pick = async (c) => {
    const chip = page.getByRole("button", { name: `${c.alias} (${REL_LABEL[c.relationship_type]})`, exact: true });
    if (await chip.count()) {
      if ((await chip.first().getAttribute("aria-pressed")) !== "true") await chip.first().click();
    } else {
      const combo = page.getByRole("combobox", { name: /상대 찾기/ }).first();
      await combo.fill(c.alias);
      await page.getByRole("option", { name: new RegExp(c.alias) }).first().click();
    }
    await page.locator(".picker-selected strong", { hasText: c.alias }).waitFor();
  };
  const compose = async (key, cond, { chipLabel = null, expectCond = cond } = {}) => {
    const c = ready(key);
    await go("compose");
    await page.getByRole("heading", { name: "첨삭", level: 1 }).waitFor();
    await pick(c);
    await page.getByLabel("초안", { exact: true }).fill(c.draft);
    if (chipLabel) {
      const chip = page.locator("button.chip:not(.chip-contact)", { hasText: chipLabel }).first();
      if ((await chip.getAttribute("aria-pressed")) !== "true") await chip.click();
    }
    const advanced = page.locator("details.advanced");
    if (!(await advanced.getAttribute("open") !== null)) await advanced.locator("summary").click();
    await choose(page.getByRole("radio", { name: new RegExp(`^${cond} `) }));
    await page.getByRole("button", { name: /^말투 맞추기/ }).click();
    const card = page.locator("article.result");
    await card.locator(".meta-line", { hasText: `조건 ${expectCond}` }).waitFor();
    state.rewrites += 1;
    await page.getByRole("heading", { name: `${c.alias}에게 맞춘 문장` }).waitFor();
    const text = (await card.locator(".result-text").innerText()).trim();
    check(text.length > 0, "추천 문장이 비어 있습니다");
    const kept = await card.locator(".kept-list li").count();
    check(kept >= 2, `지킨 항목 칩이 ${kept}개입니다`);
    check(await card.getByRole("heading", { name: "바꾼 이유" }).count(), "바꾼 이유가 없습니다");
    return { card, text, kept };
  };
  const copyResult = async ({ card, text }) => {
    await card.getByRole("button", { name: /^복사/ }).first().click();
    await card.getByText("복사함").waitFor();
    const clip = await page.evaluate(() => navigator.clipboard.readText()).catch(() => null);
    if (clip !== null) check(clip.trim() === text, "복사된 글이 추천 문장과 다릅니다");
    return clip === null ? "복사함 표시(클립보드는 읽지 못함)" : "복사한 글 = 추천 문장";
  };
  const evidence = async ({ card }) => {
    const fold = card.locator("details.fold", { hasText: "참고한 내 과거 표현" });
    await fold.locator("summary").first().click();
    await fold.locator(".evidence-text").first().waitFor();
    return `참고한 과거 표현 원문 ${await fold.locator(".evidence-text").count()}줄`;
  };

  // ---- 장면 4 ----
  const suggestion = async () => {
    const c = ready("changed");
    await go("contacts");
    await page.getByRole("link", { name: new RegExp(c.alias) }).first().click();
    await page.getByRole("heading", { name: c.alias, level: 1 }).waitFor();
    await page.getByText("말투 설정 v1").first().waitFor();
    await page.getByRole("button", { name: "과거 대화 기준 제안 보기" }).click();
    const card = page.locator(".suggestion-card-title");
    await card.waitFor();
    const title = (await card.innerText()).trim();
    const change = (await page.locator("dd", { hasText: "→" }).first().innerText()).trim();
    check(/→/.test(change), "카드에 이전 → 지금 말투가 없습니다");
    // 보기만으로는 저장되지 않는다
    check(await page.getByText("말투 설정 v1").count(), "제안을 보기만 했는데 설정 버전이 바뀌었습니다");
    return `${title} ${change}`;
  };
  const saveSuggestion = async () => {
    ready("changed");
    const apply = page.getByRole("button", { name: /말투 변화 제안 적용/ });
    if (!(await apply.count())) throw new Skipped("제안 카드가 열리지 않았습니다");
    await apply.click();
    await page.getByRole("button", { name: /^저장 \(새 버전 v2\)/ }).click();
    await page.getByText("말투 설정 v2").first().waitFor();
    return "적용 → 저장 → 말투 설정 v2";
  };

  // ---- 장면 5 ----
  const history = async () => {
    if (!state.rewrites) throw new Skipped("첨삭 결과가 없습니다");
    await go("history");
    await page.getByRole("heading", { name: "이력", level: 1 }).waitFor();
    await page.locator(".history-text").first().waitFor();
    const rows = await page.locator(".history-text").count();
    check(rows === state.rewrites, `이력 ${rows}건이 화면에서 만든 결과 ${state.rewrites}건과 다릅니다`);
    check(await page.getByText("적용", { exact: true }).count(), "복사한 결과에 '적용' 표시가 없습니다");
    await page.getByRole("button", { name: /^자세히/ }).first().click();
    await page.getByText("지킨 항목").first().waitFor();
    return `${rows}건, 자세히 보기`;
  };
  const myData = async () => {
    await go("data");
    await page.getByRole("heading", { name: "내 데이터", level: 1 }).waitFor();
    await page.getByRole("heading", { name: "상대별 요약" }).waitFor();
    const table = page.locator("table").first();
    for (const key of state.ready) await table.getByRole("link", { name: byKey[key].alias, exact: true }).waitFor();
    await table.getByText(`모두 ${state.ready.size}명`).waitFor();
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "내 데이터 내려받기" }).click(),
    ]);
    const name = download.suggestedFilename();
    check(/^persona-ize-export-\d{8}\.json$/.test(name), `내려받은 파일 이름이 다릅니다: ${name}`);
    const file = await download.path();
    const exported = JSON.parse(fs.readFileSync(file, "utf-8"));
    check(exported.contacts.length === state.ready.size, "내려받은 파일의 상대 수가 다릅니다");
    return `상대 ${state.ready.size}명, 내려받기 ${name}`;
  };

  // ---- 끝 ----
  const removeAccount = async () => {
    await go("data");
    await page.getByRole("button", { name: "계정 지우기" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.locator("input[type=password]").fill(user.password);
    await dialog.getByLabel(/확인하려면/).fill("계정 삭제");
    await dialog.getByRole("button", { name: "계정과 모든 데이터 지우기" }).click();
    await page.getByRole("button", { name: "로그인 화면" }).waitFor();
    state.deleted = true;
    const me = await page.evaluate(() => fetch("/v1/auth/me").then((r) => r.status));
    check(me === 401, `계정을 지운 뒤에도 세션이 살아 있습니다: HTTP ${me}`);
    return "계정 지움 → 로그인 화면, 세션 401";
  };

  console.log(`시연 흐름 화면 검사: ${args.baseUrl} (폭 ${args.width}px)`);
  try {
    if (await step("가입 (아이디)", signup, "0-signup")) {
      await step("다시 로그인", relogin);
      scene = "준비";
      let first = true;
      for (const c of data.contacts) {
        await step(`'${c.alias}' 추가 + 대화 가져오기`, () => importOne(c, first), `prep-import-${c.key}`);
        first = false;
      }
      scene = "1";
      let last = null;
      await step("B 첨삭 → 결과", async () => {
        last = await compose("boss", "B", { chipLabel: byKey.boss.situation });
        return `지킨 항목 ${last.kept}개`;
      }, "1-boss-B");
      await step("복사", async () => {
        if (!last) throw new Skipped("결과가 없습니다");
        return copyResult(last);
      }, "1-boss-B-copied");
      await step("C 첨삭 → 참고한 과거 표현", async () => evidence(await compose("boss", "C")), "1-boss-C");
      scene = "2";
      await step("C 첨삭", async () => `지킨 항목 ${(await compose("colleague", "C")).kept}개`);
      await step("D 첨삭", async () => `지킨 항목 ${(await compose("colleague", "D")).kept}개`, "2-colleague-D");
      scene = "3";
      await step("자동 조건 첨삭", async () => {
        const r = await compose("english", "자동", { expectCond: health.graph ? "D" : "C" });
        return `지킨 항목 ${r.kept}개`;
      }, "3-english-auto");
      scene = "4";
      await step("제안 카드", suggestion, "4-suggestion-card");
      await step("제안 적용 → 저장", saveSuggestion, "4-suggestion-saved");
      scene = "5";
      await step("이력", history, "5-history");
      await step("내 데이터 · 내려받기", myData, "5-data");
    }
  } finally {
    scene = "끝";
    if (state.registered && !state.deleted && !args.keep) {
      if (!(await step("계정 지우기", removeAccount, "end-deleted"))) {
        console.log(`       남은 임시 계정: ${user.name} — 직접 지워야 합니다.`);
      }
    }
    await browser.close();
  }

  const ok = report.failed.length === 0 && report.skipped.length === 0;
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  console.log(
    `${ok ? "화면 흐름 통과" : "화면 흐름 실패"} — 통과 ${report.passed}, 실패 ${report.failed.length}, 건너뜀 ${report.skipped.length} (${seconds}초)`,
  );
  for (const line of report.failed) console.log(`  - ${line}`);
  if (report.shots.length) console.log(`캡처 ${report.shots.length}장: ${args.out}`);
  console.log(LIMITS);
  return ok ? 0 : 1;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.log(`[실패] ${err.message}`);
    process.exit(1);
  },
);
