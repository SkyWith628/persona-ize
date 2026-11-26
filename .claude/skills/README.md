# 이 저장소의 Claude 스킬

이 폴더의 스킬은 이 저장소에서 여는 Claude Code 세션에 자동으로 잡힙니다. 둘 다 바깥에서 가져온 것이고, 받은 시점의 내용을 그대로 넣어 고정했습니다.

| 스킬 | 하는 일 | 출처 | 받은 커밋 | 라이선스 |
|---|---|---|---|---|
| `stack-diagram` | 기술 스택을 로고가 들어간 구조 그림으로 그린다 | https://github.com/caru-ini/stack-diagram | `1b37594` (2026-11-23) | MIT, 글꼴은 SIL OFL 1.1 |
| `floorplan` | 코드베이스를 눌러 볼 수 있는 지도로 만든다 | https://github.com/haydenschmitty/floorplan | `94f81b4` (2026-11-16, 0.11.0) | MIT |

## 쓰는 법

- 구조 그림: "stack-diagram으로 우리 구조를 그려 줘". 결과는 `docs/images/architecture-stack-*`에 있는 것과 같은 모양이다. Python 3.9 이상과, 로고를 받을 인터넷 연결이 필요하다.
- 코드 지도: "floorplan을 갱신해 줘". 먼저 `docs/floorplan/source/`의 `architecture.json`과 `descriptions/`를 저장소 루트의 `floorplan/`으로 복사해 두면 바뀐 파일만 다시 읽는다. Node.js 18 이상이 필요하다. 자세한 것은 `docs/floorplan/README.md`.

## 원본에서 바꾼 것

- `stack-diagram`: 그림을 공개 갤러리에 올리는 단계(`scripts/share.py`와 SKILL.md의 Step 7)를 뺐다. 비공개 프로젝트라 쓸 일이 없고, 실수로 올라가는 일을 막으려는 것이다.
- `floorplan`: `skills/floorplan/` 폴더를 그대로 넣고 저장소 루트의 LICENSE만 함께 복사했다. 플러그인 껍데기(`.claude-plugin/`)와 문서·테스트는 넣지 않았다.

## 새 버전으로 올릴 때

출처 저장소의 `skills/<이름>/` 폴더로 덮어쓰고, 위 표의 커밋을 고친다. 덮어쓰기 전에 SKILL.md와 `scripts/`에서 달라진 부분을 읽어 본다(바깥으로 무언가를 보내는 단계가 새로 생기지 않았는지). `stack-diagram`은 덮어쓴 뒤 위의 공유 단계를 다시 뺀다.

이 폴더는 lint 대상에서 빠져 있다(`ruff.toml`).
