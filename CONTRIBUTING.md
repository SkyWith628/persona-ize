# 협업 규칙

전원이 GitHub와 Docker를 쓴다. 비전공자는 **GitHub Desktop(마우스 UI)** 기준이며 터미널 명령을 요구하지 않는다.

## GitHub 흐름 (GitHub Desktop)

1. **Fetch origin** → **Current Branch → New Branch**. 이름은 `역할/내용` (예: `eval/cases-week1`, `graph/schema-v1`, `scenario/dialogs-20`).
2. 파일을 고친다 (VS Code, 또는 CSV는 엑셀/구글시트에서 편집 후 **CSV UTF-8**로 저장).
3. Desktop 왼쪽에서 바뀐 파일 확인 → Summary에 한 줄 → **Commit to <branch>**.
4. **Publish branch** → **Create Pull Request** → 팀장을 Reviewer로.
5. 팀장이 리뷰 후 병합. `main`은 직접 수정하지 않는다.

문서·CSV 한 줄 수정은 GitHub 웹에서 연필 아이콘으로 바로 고치고 "Create a new branch and start a pull request"를 고른다.

## 폴더 소유 (충돌 방지)

| 폴더 | 주 담당 | 다른 사람이 수정할 때 |
|---|---|---|
| `backend/`, `docker/`, `apps/` | 팀장 | PR 필수 |
| `backend/app/prompts/` | 시나리오(첨삭 문구), 평가(검증 문구) | 팀장이 코드 연결 확인 |
| `evals/` | 평가 | |
| `data/synthetic/` | 시나리오 | 그래프 담당이 요청사항 추가 |
| `graph/` | 지식그래프 | |
| `docs/` | 전원 | |

## 내 역할은?
매주 무엇을 어떻게 만드는지: [docs/roles/scenario.md](docs/roles/scenario.md)(사용자·시나리오) · [docs/roles/evaluation.md](docs/roles/evaluation.md)(평가·품질) · [docs/roles/graph.md](docs/roles/graph.md)(지식그래프). 각 폴더의 `CLAUDE.md`는 그 폴더에서 Claude Code를 쓸 때의 규칙이다.

## 파일 양식
- 평가 사례·합성 대화를 쓰는 법(비전공자용, 열 설명·예시 포함): [docs/dataset-guide.md](docs/dataset-guide.md).
- CSV는 **UTF-8**, 첫 줄은 열 이름. 열 이름을 바꾸면 코드가 깨지니 PR에 적는다. `python scripts/validate_data_files.py`가 CI에서 검사한다.
- 합성 대화 JSON 형식: [data/synthetic/README.md](data/synthetic/README.md).
- 프롬프트 `.md`는 `{placeholder}`를 지우지 않는다.

## 비밀정보
- `.env`는 커밋하지 않는다 (`.gitignore`). API 키는 팀 공용 한도 키를 비밀번호 관리자로 공유한다.
- 커밋 전 diff에서 키·토큰·실제 대화가 없는지 본다. **합성 데이터만** 저장소에 넣는다.

## Docker
- `make up`으로 PostgreSQL(pgvector)·Neo4j가 뜨고 `make dev`로 백엔드(화면 http://localhost:8000). 전체를 컨테이너로 띄우려면 `make up-all`(이미지 2.3GB). Docker Desktop 메모리 4GB 이상 권장. Windows 선행 조건은 `docker/README.md`.
- 그래프 담당은 Neo4j Browser(http://localhost:7474)에서 팀장이 준 Cypher를 실행·수정한다.

## 점검 시점
- 월요일: 목표 확인 + 논문 발표(1인 1편, `docs/literature-review.md`).
- 금요일: 진행 점검, 다음 주 사례 배분.
