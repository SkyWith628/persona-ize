"""배포 설정 점검: `.env.prod`가 공개 서버에 올려도 되는 값인지 본다.

    python scripts/check_prod_env.py .env.prod

문제가 있으면 항목을 모두 출력하고 1로 끝난다. 값 자체(키·비밀번호)는 출력하지 않는다.
표준 라이브러리만 쓰므로 가상환경이 없는 서버에서도 돈다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "app" / "tools"))

from prod_env import find_problems, parse_env  # noqa: E402


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else ".env.prod")
    if not path.is_file():
        print(f"{path} 파일이 없습니다. .env.prod.example을 복사해 채우세요.")
        return 2
    problems = find_problems(parse_env(path.read_text(encoding="utf-8")))
    for problem in problems:
        print(f"✗ {problem}")
    print(f"{path}: 문제 {len(problems)}건" if problems else f"{path}: 배포 설정 점검 통과")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
