# Persona_ize

mode: work

상대·상황에 맞는 사용자 말투로 메시지를 첨삭하되, 원문의 사실과 의도를 보존하는 서비스. 공모전(2026-12 말, 공고 미정) + 포트폴리오 겸용.

## 기간·팀
- 2026-11-02(월) ~ 12-20(일) 주, 7주. 공모전 공고가 나오면 요건에 맞춰 범위를 재조정한다.
- 4명: 팀장 겸 개발 1(전공), 비전공 3(아이디어 2, 지식그래프 관심 1). 주당 40~50시간 가정.
- 역할·일정: [docs/team-roles-and-schedule.md](docs/team-roles-and-schedule.md)

## 범위 원칙
- 기준 설계: `docs/project-design.md`(팀 설계). 충돌하면 그쪽을 따른다. `docs/design.md`는 그래프 실험 보완.
- **지식그래프(D)는 필수 과제.** 단순 검색(C)과 그래프 검색(D)을 같은 평가셋에서 나란히 비교한다.
- 실험 조건: A 페르소나만 / B 페르소나+최근 맥락 / C +벡터 검색 과거 대화 / D +그래프 검색.
- 사실 보존 실패는 다른 점수로 상쇄하지 않는다. 검증 실패 시 원문 유지, 자동 전송 없음.
- 합성 데이터만 외부 서비스(OpenAI, Neo4j Aura)에 보낸다. 실제 대화는 넣지 않는다.

## 명령어 (spikes/graphiti-p0, 상세는 해당 README)
- 테스트: `.venv/bin/python -m pytest -q`  (README의 `Scripts/python.exe`는 Windows 기준, 이 맥에서는 `bin/`)
- dry-run: `.venv/bin/python ingest.py --dry-run`
- 5개만 수집: `.venv/bin/python ingest.py --limit 5`

## 함정
- 이 폴더는 iCloud 안에 있고 아직 git 저장소가 아니다. iCloud 동기화는 `.git`과 Docker 볼륨을 깨뜨릴 수 있으므로, 앱 저장소는 **iCloud 밖**(예: `~/dev/persona-ize`)에 만들어 GitHub와 연결한다. `.venv`, `.env`, `runs/`는 `.gitignore`.
- 협업: 전원이 GitHub(브랜치→PR→팀장 리뷰)와 Docker(`docker compose up`)를 쓴다. 비전공자는 GitHub Desktop 기준, 규칙은 [docs/team-roles-and-schedule.md](docs/team-roles-and-schedule.md).
- Neo4j는 로컬 Docker 컨테이너를 기본으로 한다(Aura는 스파이크 용도).
- 스파이크의 실제 API 호출은 아직 미검증. `.env`(Aura, OpenAI 키)는 커밋 금지.
- API 비용은 구독($20/월)과 별개로 과금된다. 사용량 한도를 먼저 걸고 `--limit 5`로 비용을 측정한다.
