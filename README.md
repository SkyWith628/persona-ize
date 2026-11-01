# Persona_ize

상대와 상황에 맞는 말투로 메시지를 첨삭하되, 원문의 사실과 의도를 보존하는 서비스. 지식그래프(D) 검색과 일반 벡터 검색(C)을 비교하는 것이 핵심 실험이다.

- 설계: [docs/project-design.md](docs/project-design.md), [docs/design.md](docs/design.md)
- 역할·일정·협업 규칙: [docs/team-roles-and-schedule.md](docs/team-roles-and-schedule.md)
- 프로젝트 규칙(AI 도우미용 포함): [CLAUDE.md](CLAUDE.md)

## 시작하기 (전원 공통)

필요한 것: Git(GitHub Desktop 가능), Docker Desktop.

```bash
make up
```

- 백엔드 상태: http://localhost:8000/health
- Neo4j Browser: http://localhost:7474 (사용자 `neo4j`, 비밀번호는 `.env`의 `NEO4J_PASSWORD`)

```bash
make test
```

```bash
make down
```

## 일하는 방법
1. 브랜치를 만든다: `역할/내용` (예: `eval/cases-week1`)
2. 자기 폴더의 파일을 수정한다 (폴더별 담당은 위 역할 문서 참고)
3. Pull Request를 올리고 팀장에게 리뷰를 요청한다
4. `main`은 직접 수정하지 않는다

## 폴더
| 폴더 | 내용 |
|---|---|
| `backend/` | API, 첨삭 워크플로, 프롬프트 (`app/prompts/`) |
| `evals/` | 평가 사례와 결과 |
| `data/synthetic/` | 합성 대화 (실제 대화 금지) |
| `graph/` | 그래프 스키마와 점검 사례 |
| `spikes/` | 사전 실험 (본 앱과 분리) |
| `docs/` | 설계·결정 기록 |
