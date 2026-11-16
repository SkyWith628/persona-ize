// 클립보드 복사. 실패할 수 있으므로(포커스 없음, 권한 없음, 보안 맥락 아님) 성공 여부만 돌려준다.
// 피드백 저장 같은 다른 일은 복사 성공과 묶지 않는다(CLAUDE.md 함정).

function legacyCopy(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.setAttribute("aria-hidden", "true");
  area.className = "sr-only";
  document.body.append(area);
  const previous = document.activeElement;
  let ok = false;
  try {
    area.select();
    area.setSelectionRange(0, text.length);
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  if (previous instanceof HTMLElement) previous.focus();
  return ok;
}

export async function copyText(text) {
  const value = String(text ?? "");
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* 아래 예전 방식으로 한 번 더 */
  }
  return legacyCopy(value);
}

/** 복사하지 못했을 때 사용자가 바로 Ctrl+C 할 수 있게 글자를 선택해 둔다. */
export function selectContents(el) {
  if (!el) return;
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
    el.focus();
    el.select();
    return;
  }
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}
