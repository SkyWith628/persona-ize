# docs 안내

어떤 문서를 먼저 봐야 하는지 적어 둔 목록입니다. 문서끼리 어긋나면 `project-design.md`가 맞습니다.

## 설계

| 문서 | 내용 |
|---|---|
| [project-design.md](design/project-design.md) | 팀 설계 원본. 기준 문서 |
| [design.md](design/design.md) | 전체 흐름과 그래프 실험(D) 보완 |
| [graph-redesign-2026-11-16.md](design/graph-redesign-2026-11-16.md) | 지식 그래프 재설계와 말투 프로필 |
| [knowledge-graph.md](design/knowledge-graph.md) | 지식 그래프 구조와 쓰는 법 |
| [ml-design-2026-11-24.md](design/ml-design-2026-11-24.md) | 학습 모델 도입 제안 (그림 원본: [ml-overview.drawio](design/ml-overview.drawio)) |
| [literature-review.md](design/literature-review.md) | 참고한 논문 정리 |

## 운영과 진행

| 문서 | 내용 |
|---|---|
| [decisions.md](project/decisions.md) | 결정 기록 |
| [backlog.md](project/backlog.md) | 남은 일 |
| [deploy.md](guides/deploy.md) | 배포와 운영 |
| [api.md](guides/api.md) | API 명세 |
| [dataset-guide.md](guides/dataset-guide.md) | 평가셋과 합성 데이터 형식 |
| [demo-script.md](guides/demo-script.md) | 시연 순서 |
| [iphone-mobile.md](design/iphone-mobile.md) | 아이폰 화면 설계 |

## 팀

| 문서 | 내용 |
|---|---|
| [team-roles-and-schedule.md](project/team-roles-and-schedule.md) | 역할과 일정 |
| [roles/](project/roles/) | 담당별 안내 (평가, 그래프, 시나리오) |

## 그림

- 구조 그림: [images/architecture-stack-ko.png](images/architecture-stack-ko.png) (영문은 `-en`, 어두운 배경은 `-dark`)
- 학습 모델 제안의 그림: `images/ml-overview-*`
- 코드 지도: [floorplan/persona-ize-map.html](floorplan/persona-ize-map.html) — 받아서 브라우저로 열면 파일 하나하나까지 눌러 볼 수 있습니다. 설명은 [floorplan/README.md](floorplan/README.md)

## archive

지금은 기준으로 쓰지 않는 문서와 예전 그림을 [archive/](archive/)로 옮겼습니다. 지운 것은 없습니다.

| 파일 | 옮긴 이유 |
|---|---|
| `architecture_v2.drawio`, `architecture_v2.jpg`, `pipeline.drawio` | 예전 설계도. 구조 그림으로 대신합니다 |
| `status-2026-11-10.md` | 11/10 회의 자료 |
| `wireframe-review-2026-11-10.md`, `wireframes-v2.html` | 11/10 와이어프레임 검토와 수정안 화면 |
| `extension-design.md` | 브라우저 확장 설계. 아직 만들지 않았고 위 와이어프레임 문서를 근거로 합니다 |
