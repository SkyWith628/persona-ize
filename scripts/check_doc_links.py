"""문서 링크 검사 — 마크다운의 상대 경로 링크·이미지가 실제 파일·폴더를 가리키는지 본다.

표준 라이브러리만 쓴다. 저장소 루트에서:

    python3 scripts/check_doc_links.py                    # 추적되는 .md 전부 (docs/archive/ 제외)
    python3 scripts/check_doc_links.py --include-archive  # 예전 기록까지
    python3 scripts/check_doc_links.py --exclude evals/results/

깨진 링크는 `파일:줄: 경로`로 한 줄씩 찍고 종료 코드 1. 없으면 0.

보는 것
- `[글](경로)` · `![그림](경로)` · `[글](<공백 있는 경로>)` · `[글](경로 "제목")` · 참조 정의 `[이름]: 경로`
  · 마크다운 안의 HTML `<img src="경로">` · `<a href="경로">`
- `경로#앵커`·`경로?질의`는 경로만 본다. `%20`·`%EA%B0%80` 같은 퍼센트 인코딩은 풀어서 본다.
- `/`로 시작하는 경로는 저장소 루트 기준(GitHub와 같다), 나머지는 그 문서가 있는 폴더 기준.

보지 않는 것
- 외부 주소(`http:`·`https:`·`mailto:` 등 스킴이 있는 것, `//호스트`)와 `#앵커`만 있는 링크.
- **앵커가 실제 제목에 있는지** — 한글 제목을 앵커로 바꾸는 규칙이 뷰어(GitHub·VS Code·Obsidian)마다 다르다.
- 코드 블록(``` · ~~~)과 인라인 코드(`…`) 안의 것 — 예시로 적은 링크다.
- `docs/archive/` — 예전 기록이라 옮겨진 문서를 가리키는 링크가 남아 있다(`--include-archive`로 포함).
- 백틱으로 적은 경로(`docs/guides/deploy.md`처럼 누를 수 없는 글자). 눌러서 가는 링크만 본다.

대상 파일은 `git ls-files '*.md'`(추적되는 것만 — `.venv`·작업 트리 같은 것이 섞이지 않는다). git이 없거나
저장소가 아니면 폴더를 돌며 찾는다(숨김 폴더·`node_modules`는 건너뛴다).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

DEFAULT_EXCLUDES = ("docs/archive/",)
SKIP_DIRS = frozenset({"node_modules", "__pycache__"})

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_REFERENCE = re.compile(r" {0,3}\[(?!\^)[^\]]+\]:[ \t]*(<[^>]*>|\S+)")
_HTML_ATTR = re.compile(
    r"""<(?:img|a|source|video)\b[^>]*?\b(?:src|href)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I
)
_CODE_SPAN = re.compile(r"(`+)(.+?)\1(?!`)")


@dataclass(frozen=True)
class Broken:
    file: str  # 저장소 루트 기준, `/` 구분
    line: int
    target: str  # 문서에 적힌 그대로

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.target}"


def _blank_code_spans(line: str) -> str:
    """인라인 코드를 같은 길이의 공백으로 바꾼다 (안에 적힌 `[글](경로)`는 예시다)."""
    return _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)


def _inline_targets(line: str) -> list[str]:
    """`[글](경로)`의 경로들. 경로 안의 짝 맞는 괄호와 `<꺾쇠>` 꼴을 읽는다."""
    found: list[str] = []
    start = 0
    while True:
        at = line.find("](", start)
        if at < 0:
            return found
        start = at + 2
        if "[" not in line[:at]:
            continue
        i = start
        while i < len(line) and line[i] in " \t":
            i += 1
        if i < len(line) and line[i] == "<":
            end = line.find(">", i)
            if end > 0:
                found.append(line[i + 1 : end])
                start = end + 1
            continue
        depth = 0
        j = i
        while j < len(line):
            ch = line[j]
            if ch in " \t":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        if j > i:
            found.append(line[i:j])
        start = j


def link_targets(text: str) -> list[tuple[int, str]]:
    """문서 본문에서 (줄 번호, 링크 대상)들. 코드 블록·인라인 코드 안은 건너뛴다."""
    out: list[tuple[int, str]] = []
    fence: str | None = None  # 열려 있는 코드 블록의 울타리 (``` 또는 ~~~, 길이 포함)
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _FENCE.match(raw)
        if fence is not None:
            # 닫는 울타리: 같은 글자, 연 것 이상의 길이, 뒤에 다른 글자 없음
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                if not match.group(2).strip():
                    fence = None
            continue
        if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            fence = match.group(1)
            continue
        line = _blank_code_spans(raw)
        reference = _REFERENCE.match(line)
        if reference:
            out.append((number, reference.group(1).strip("<>")))
        out.extend((number, target) for target in _inline_targets(line))
        out.extend((number, m.group(1) or m.group(2) or "") for m in _HTML_ATTR.finditer(line))
    return out


def local_path(target: str) -> str | None:
    """검사할 상대 경로(앵커·질의를 떼고 퍼센트 인코딩을 푼 것). 외부 주소·앵커만 있는 링크는 None."""
    target = target.strip()
    if not target or target.startswith(("#", "//")) or _SCHEME.match(target):
        return None
    path = target.split("#", 1)[0].split("?", 1)[0]
    return unquote(path) or None


def check_file(root: Path, relative: str) -> list[Broken]:
    source = root / relative
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return [Broken(relative, 0, "(읽을 수 없는 파일)")]
    broken: list[Broken] = []
    for number, target in link_targets(text):
        path = local_path(target)
        if path is None:
            continue
        resolved = root / path.lstrip("/") if path.startswith("/") else source.parent / path
        if not resolved.exists():
            broken.append(Broken(relative, number, target))
    return broken


def tracked_markdown(root: Path) -> list[str] | None:
    """`git ls-files '*.md'`. git이 없거나 저장소가 아니면 None."""
    try:
        done = subprocess.run(  # noqa: S603 - 고정된 명령
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],  # noqa: S607
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [name for name in done.stdout.decode("utf-8").split("\0") if name]


def walked_markdown(root: Path) -> list[str]:
    found: list[str] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and (not d.startswith(".") or d == ".github"))
        for name in sorted(files):
            if name.endswith(".md"):
                found.append((Path(folder) / name).relative_to(root).as_posix())
    return found


def markdown_files(root: Path, excludes: tuple[str, ...], *, use_git: bool = True) -> list[str]:
    files = tracked_markdown(root) if use_git else None
    if files is None:
        files = walked_markdown(root)
    prefixes = tuple(e.strip("/") + "/" for e in excludes if e.strip("/"))
    # 추적되지만 작업 트리에서 지워진 파일은 건너뛴다 (커밋 전의 삭제)
    return sorted(f for f in files if not f.startswith(prefixes) and (root / f).is_file())


def check(root: Path, excludes: tuple[str, ...] = DEFAULT_EXCLUDES, *, use_git: bool = True) -> list[Broken]:
    broken: list[Broken] = []
    for relative in markdown_files(root, excludes, use_git=use_git):
        broken.extend(check_file(root, relative))
    return broken


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="마크다운 상대 링크 검사 (표준 라이브러리만)")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="저장소 루트")
    p.add_argument("--include-archive", action="store_true", help="docs/archive/ 도 본다 (기본은 제외)")
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PREFIX",
        help="건너뛸 경로 앞부분 (루트 기준, 여러 번 줄 수 있다). 예) evals/results/",
    )
    p.add_argument("--no-git", action="store_true", help="git을 쓰지 않고 폴더를 돌며 .md를 찾는다")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    excludes = (*(() if args.include_archive else DEFAULT_EXCLUDES), *args.exclude)
    files = markdown_files(root, excludes, use_git=not args.no_git)
    broken = [b for relative in files for b in check_file(root, relative)]
    for item in broken:
        print(item)
    skipped = f", 제외 {' '.join(excludes)}" if excludes else ""
    if broken:
        print(f"깨진 링크 {len(broken)}건 (문서 {len(files)}개{skipped})")
        return 1
    print(f"깨진 링크 없음 (문서 {len(files)}개{skipped})")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔에서도 한글이 깨지지 않게
    raise SystemExit(main())
