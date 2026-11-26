"""문서 링크 검사(`scripts/check_doc_links.py`) — 마크다운 상대 링크가 실제 파일을 가리키는지.

임시 폴더에 문서를 만들어 본다(git 없이 폴더를 도는 경로). 마지막 테스트만 이 저장소의 문서를 본다 —
CI의 `data-files` 잡이 같은 검사를 스크립트로 돈다.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_doc_links.py"


def _load():  # noqa: ANN202
    if not SCRIPT.is_file():
        pytest.skip("scripts/check_doc_links.py가 없는 설치입니다 (backend만 복사한 이미지).")
    spec = importlib.util.spec_from_file_location("pz_check_doc_links", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass가 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


links = _load()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _broken(root: Path, **kwargs) -> list[str]:  # noqa: ANN003
    return [str(b) for b in links.check(root, use_git=False, **kwargs)]


def test_working_links_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "README.md": "\n".join(
                [
                    "# 문서",
                    "[설계](docs/design/design.md) · [폴더](docs/guides/) · [폴더2](docs/guides)",
                    "[앵커](docs/design/design.md#실패-유형-점검) · [질의](docs/design/design.md?plain=1)",
                    "[이 문서의 절](#문서) · [밖](https://example.com/a.md) · [메일](mailto:a@example.com)",
                    "[루트 기준](/docs/design/design.md) · [스킴 없는 주소](//example.com/x.md)",
                    "![그림](docs/images/screen%20shot.png) · ![한글](docs/images/화면.png)",
                    "[공백](<docs/guides/배포 안내.md>) · [괄호](docs/a(1).md)",
                    "[제목](docs/guides/a.md '제목')",
                    '<img src="docs/images/화면.png" alt="화면" width="300">',
                    "[![배지](docs/images/화면.png)](docs/guides/a.md)",
                    "[참조][설계서]",
                    "",
                    "[설계서]: docs/design/design.md",
                    "[^1]: 각주는 링크가 아니다.md",
                ]
            ),
            "docs/design/design.md": "[위로](../../README.md) · [옆](../guides/a.md)\n",
            "docs/guides/a.md": "",
            "docs/guides/배포 안내.md": "",
            "docs/a(1).md": "",
            "docs/images/screen shot.png": "",
            "docs/images/화면.png": "",
        },
    )
    assert _broken(tmp_path) == []


def test_broken_links_are_reported_with_file_and_line(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "README.md": "첫 줄\n[없음](docs/missing.md) 그리고 [있음](docs/a.md)\n\n![그림](img/none.png)\n",
            "docs/a.md": "\n".join(
                [
                    "[옛 자리](../design.md#절)",  # 앵커를 떼고 본 경로가 없다
                    "[공백](<없는 문서.md>)",
                    "[퍼센트](%EC%97%86%EC%9D%8C.md)",
                    '<a href="gone/">폴더</a>',
                    "[정의]: ./nowhere.md",
                    "[루트 기준](/docs/b.md)",
                ]
            ),
        },
    )
    assert _broken(tmp_path) == [
        "README.md:2: docs/missing.md",
        "README.md:4: img/none.png",
        "docs/a.md:1: ../design.md#절",
        "docs/a.md:2: 없는 문서.md",
        "docs/a.md:3: %EC%97%86%EC%9D%8C.md",
        "docs/a.md:4: gone/",
        "docs/a.md:5: ./nowhere.md",
        "docs/a.md:6: /docs/b.md",
    ]


def test_code_blocks_and_inline_code_are_skipped(tmp_path: Path) -> None:
    text = "\n".join(
        [
            "링크 쓰는 법: `[글](없는/경로.md)` 그리고 ``[글](또 `없는`.md)``",
            "```markdown",
            "[예시](no/such/file.md)",
            "```",
            "~~~",
            "[예시](no/such/file.md)",
            "```",  # 다른 울타리는 닫지 못한다
            "[예시](no/such/file.md)",
            "~~~",
            "````",
            "```",  # 더 짧은 울타리는 닫지 못한다
            "[예시](no/such/file.md)",
            "````",
            "    ```",  # 네 칸 들여쓰기는 울타리가 아니다
            "[블록 밖](real-missing.md)",
        ]
    )
    _write(tmp_path, {"a.md": text})
    assert _broken(tmp_path) == ["a.md:15: real-missing.md"]


def test_an_unclosed_code_block_hides_the_rest_of_that_file_only(tmp_path: Path) -> None:
    _write(tmp_path, {"a.md": "```\n[안](x.md)\n", "b.md": "[밖](x.md)\n"})
    assert _broken(tmp_path) == ["b.md:1: x.md"]


def test_archive_is_excluded_by_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "docs/archive/old.md": "[옮겨진 문서](../design.md)\n",
            "docs/archived.md": "[이름만 비슷한 문서](none.md)\n",
            "evals/results/r.md": "[결과](none.csv)\n",
        },
    )
    assert _broken(tmp_path) == ["docs/archived.md:1: none.md", "evals/results/r.md:1: none.csv"]
    assert _broken(tmp_path, excludes=()) == [
        "docs/archive/old.md:1: ../design.md",
        "docs/archived.md:1: none.md",
        "evals/results/r.md:1: none.csv",
    ]
    assert _broken(tmp_path, excludes=("docs/archive/", "evals/results")) == ["docs/archived.md:1: none.md"]


def test_walk_skips_hidden_and_vendored_folders(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            ".venv/lib/pkg/README.md": "[x](none.md)\n",
            "node_modules/pkg/README.md": "[x](none.md)\n",
            ".github/pull_request_template.md": "[x](none.md)\n",
            "notes.txt": "[x](none.md)\n",
        },
    )
    assert links.markdown_files(tmp_path, (), use_git=False) == [".github/pull_request_template.md"]


def test_git_lists_only_tracked_files(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)  # noqa: S603, S607

    try:
        git("init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git을 쓸 수 없는 환경입니다.")
    _write(tmp_path, {"문서/추적.md": "[x](none.md)\n", "untracked.md": "[x](none.md)\n", "gone.md": ""})
    git("add", "문서/추적.md", "gone.md")
    (tmp_path / "gone.md").unlink()  # 추적되지만 작업 트리에서 지운 파일
    assert links.markdown_files(tmp_path, ()) == ["문서/추적.md"]
    assert [str(b) for b in links.check(tmp_path, ())] == ["문서/추적.md:1: none.md"]


def test_main_prints_broken_links_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, {"a.md": "[x](none.md)\n", "docs/archive/old.md": "[y](gone.md)\n"})
    assert links.main(["--root", str(tmp_path), "--no-git"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "a.md:1: none.md",
        "깨진 링크 1건 (문서 1개, 제외 docs/archive/)",
    ]
    assert links.main(["--root", str(tmp_path), "--no-git", "--include-archive"]) == 1
    assert capsys.readouterr().out.splitlines()[:2] == ["a.md:1: none.md", "docs/archive/old.md:1: gone.md"]
    (tmp_path / "none.md").write_text("", encoding="utf-8")
    assert links.main(["--root", str(tmp_path), "--no-git"]) == 0
    assert capsys.readouterr().out.strip() == "깨진 링크 없음 (문서 2개, 제외 docs/archive/)"


def test_this_repository_has_no_broken_doc_links() -> None:
    """문서를 옮기거나 지우면 여기서 걸린다 (2026-11-25에 docs/를 design·guides·project로 나눴다)."""
    if not (ROOT / "docs").is_dir():
        pytest.skip("docs/가 없는 설치입니다 (backend만 복사한 이미지).")
    broken = links.check(ROOT)
    assert broken == [], "\n".join(str(b) for b in broken)
