# docs 안내

문서를 쓰임새별 폴더로 나눠 두었습니다. 문서끼리 어긋나면 `design/project-design.md`가 맞습니다.

| 폴더 | 무엇이 들어 있나 |
|---|---|
| [design/](design/) | 무엇을 어떻게 만들지 적은 설계 문서 |
| [guides/](guides/) | 따라 하면서 쓰는 안내서 (배포, API, 데이터 형식, 시연) |
| [project/](project/) | 프로젝트 진행 기록과 팀 운영 (결정, 남은 일, 역할) |
| [images/](images/) | 문서에 들어가는 그림 |
| [floorplan/](floorplan/) | 눌러 볼 수 있는 코드 지도 |
| [archive/](archive/) | 지금은 기준으로 쓰지 않는 예전 문서와 그림 |

## design — 설계

| 문서 | 내용 |
|---|---|
| [project-design.md](design/project-design.md) | 팀 설계 원본. 기준 문서 |
| [design.md](design/design.md) | 전체 흐름과 그래프 실험(D) 보완 |
| [graph-redesign-2026-11-16.md](design/graph-redesign-2026-11-16.md) | 지식 그래프 재설계와 말투 프로필 |
| [knowledge-graph.md](design/knowledge-graph.md) | 지식 그래프 구조와 쓰는 법 |
| [ml-design-2026-11-24.md](design/ml-design-2026-11-24.md) | 학습 모델 도입 제안 (그림 원본: [ml-overview.drawio](design/ml-overview.drawio)) |
| [ml-judge-design-2026-11-25.md](design/ml-judge-design-2026-11-25.md) | 학습 모델 ① 의미 보존 판정 모델 설계 (설계도 포함) |
| [ml-judge-bench-2026-11-25.md](design/ml-judge-bench-2026-11-25.md) | 판정 모델 기반 모델 측정: GPU 메모리·학습 속도·CPU 판정 시간 (후보 모델 5개) |
| [ml-prefilter-design-2026-11-25.md](design/ml-prefilter-design-2026-11-25.md) | 학습 모델 ② 그래프 수집 전 거르기 설계 (설계도 포함) |
| [ml-style-sim-design-2026-11-25.md](design/ml-style-sim-design-2026-11-25.md) | 학습 모델 ③ 문체 유사도 모델 설계 (설계도 포함) |
| [ml-condition-selector-design-2026-11-25.md](design/ml-condition-selector-design-2026-11-25.md) | 학습 모델 ④ 조건 선택기 설계 (설계도 포함) |
| [iphone-mobile.md](design/iphone-mobile.md) | 아이폰 화면 설계 |
| [literature-review.md](design/literature-review.md) | 참고한 논문 정리 |

## guides — 안내서

| 문서 | 내용 |
|---|---|
| [deploy.md](guides/deploy.md) | 배포와 운영 |
| [api.md](guides/api.md) | API 명세 |
| [dataset-guide.md](guides/dataset-guide.md) | 평가셋과 합성 데이터 형식 |
| [demo-script.md](guides/demo-script.md) | 시연 순서 |

## project — 진행과 팀

| 문서 | 내용 |
|---|---|
| [decisions.md](project/decisions.md) | 결정 기록 |
| [backlog.md](project/backlog.md) | 남은 일 |
| [team-roles-and-schedule.md](project/team-roles-and-schedule.md) | 역할과 일정 |
| [roles/](project/roles/) | 담당별 안내 (평가, 그래프, 시나리오) |

## images — 그림

- 구조 그림: [architecture-stack-ko.png](images/architecture-stack-ko.png) (영문은 `-en`, 어두운 배경은 `-dark`, 원본은 같은 이름의 `.html`)
- 학습 모델 제안의 그림: `ml-overview-*`
- 학습 모델 네 개의 설계도: `ml-judge-stack-ko`, `ml-prefilter-stack-ko`, `ml-style-sim-stack-ko`, `ml-condition-selector-stack-ko` (각각 `.html` 원본과 `.png` 두 장)
- 판정 모델 측정 보고서의 그림: `ml-judge-bench-{flow,gpu-memory,cpu-latency,server-memory}` (각각 `.svg` 원본과 `.png`)

## floorplan — 코드 지도

[persona-ize-map.html](floorplan/persona-ize-map.html)을 받아서 브라우저로 열면 파일 하나하나까지 눌러 볼 수 있습니다. 읽는 법과 다시 만드는 법은 [floorplan/README.md](floorplan/README.md)에 있습니다.

## archive — 예전 것

지운 것은 없고 옮겨 두기만 했습니다.

| 파일 | 옮긴 이유 |
|---|---|
| `architecture_v2.drawio`, `architecture_v2.jpg`, `pipeline.drawio` | 예전 설계도. 구조 그림으로 대신합니다 |
| `status-2026-11-10.md` | 11/10 회의 자료 |
| `wireframe-review-2026-11-10.md`, `wireframes-v2.html` | 11/10 와이어프레임 검토와 수정안 화면 |
| `extension-design.md` | 브라우저 확장 설계. 아직 만들지 않았고 위 와이어프레임 문서를 근거로 합니다 |

## 새 문서를 넣을 때

- 설계나 제안이면 `design/`, 따라 하는 절차면 `guides/`, 회의·결정·일정이면 `project/`에 둡니다.
- 문서에 넣을 그림은 `images/`에 두고 상대 경로(`../images/...`)로 겁니다.
- 더 쓰지 않게 된 문서는 지우지 말고 `archive/`로 옮기고, 위 표에 이유를 한 줄 적습니다.
