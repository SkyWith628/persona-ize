// 해시 라우팅의 순수 부분(주소 ↔ 화면 이름). 테스트: tests/router.test.js

export const ROUTES = [
  { name: "compose", pattern: "/compose", title: "첨삭" },
  { name: "contacts", pattern: "/contacts", title: "상대" },
  { name: "contact", pattern: "/contacts/:id", title: "상대", nav: "contacts" },
  { name: "import", pattern: "/import", title: "대화 가져오기" },
  { name: "history", pattern: "/history", title: "이력" },
  { name: "data", pattern: "/data", title: "내 데이터" },
  { name: "settings", pattern: "/settings", title: "설정" },
];

export const DEFAULT_ROUTE = "compose";

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** "#/contacts/abc?tab=x" → { path: "/contacts/abc", segments: ["contacts","abc"], query: {tab:"x"} } */
export function parseHash(hash) {
  let raw = String(hash ?? "");
  if (raw.startsWith("#")) raw = raw.slice(1);
  const qAt = raw.indexOf("?");
  const pathPart = qAt >= 0 ? raw.slice(0, qAt) : raw;
  const queryPart = qAt >= 0 ? raw.slice(qAt + 1) : "";
  const segments = pathPart.split("/").filter(Boolean).map(safeDecode);
  const query = {};
  for (const pair of queryPart.split("&")) {
    if (!pair) continue;
    const eq = pair.indexOf("=");
    const key = safeDecode(eq >= 0 ? pair.slice(0, eq) : pair);
    if (key) query[key] = safeDecode(eq >= 0 ? pair.slice(eq + 1) : "");
  }
  return { path: `/${segments.join("/")}`, segments, query };
}

/** 주소 → { name, params, query, title, nav }. 맞는 화면이 없으면 null. */
export function matchRoute(hash, routes = ROUTES) {
  const { segments, query } = parseHash(hash);
  for (const route of routes) {
    const want = route.pattern.split("/").filter(Boolean);
    if (want.length !== segments.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < want.length; i += 1) {
      if (want[i].startsWith(":")) params[want[i].slice(1)] = segments[i];
      else if (want[i] !== segments[i]) {
        ok = false;
        break;
      }
    }
    if (ok) return { name: route.name, params, query, title: route.title, nav: route.nav || route.name };
  }
  return null;
}

/** buildHash("contact", {id:"abc"}, {tab:"x"}) → "#/contacts/abc?tab=x" */
export function buildHash(name, params = {}, query = {}, routes = ROUTES) {
  const route = routes.find((r) => r.name === name);
  if (!route) throw new Error(`없는 화면 이름: ${name}`);
  const path = route.pattern
    .split("/")
    .filter(Boolean)
    .map((seg) => {
      if (!seg.startsWith(":")) return seg;
      const value = params[seg.slice(1)];
      if (value === undefined || value === null || value === "") throw new Error(`주소 값이 없습니다: ${seg}`);
      return encodeURIComponent(String(value));
    })
    .join("/");
  const pairs = Object.entries(query)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return `#/${path}${pairs.length ? `?${pairs.join("&")}` : ""}`;
}
