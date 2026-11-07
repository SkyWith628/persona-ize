# 그래프 스키마 초안 v0

작성: 지식그래프 담당 (초안은 팀장이 스파이크 코드에서 옮김, 2026-11-08). 1주차 말까지 검토·보완한다.
코드: [`graphiti_p0/p0/schema.py`](graphiti_p0/p0/schema.py). 이 문서를 고치면 PR에 코드 반영 요청을 적는다.

## 격리 규칙
- `group_id = "user:{user_id}"` — 사용자 단위로 그래프를 분리한다. 다른 사용자 그래프는 검색하지 않는다.
- 모든 노드·엣지는 출처 에피소드(메시지 id)를 가진다. 원문 삭제 시 파생 노드·엣지도 삭제한다.
- 추출된 사실과 사용자가 직접 확정한 사실(페르소나 설정)은 구분한다. 그래프의 `speech_style`은 **참고**이며 설정을 덮어쓰지 않는다.

## 엔티티

| 타입 | 설명 | 속성 | 예 |
|---|---|---|---|
| Person | 대화에 등장하는 사람 (본인·상대·제3자) | `role` 직업·직책·가족 관계 | 박지현(팀장), 김준호(동생) |
| Event | 시점이 있는 사건·약속·상태 변화 | `event_type` request/refusal/apology/schedule/promise/conflict/status/other · `status` open/resolved · `importance` 1~10 | "10/5 일정 조율 갈등", "11/5까지 워크숍 자료 약속" |

## 관계 (엣지)

| 타입 | source → target | 속성 | 예 |
|---|---|---|---|
| Relationship | Person → Person (방향: source가 target을 어떻게 대하나) | `relation_type` family/friend/colleague/superior/subordinate/partner/acquaintance · `speech_style` 반말/존댓말/혼용 · `address_term` 호칭 · `closeness` 1~5 | 김민수→박지현: superior, 존댓말, "팀장님" |
| Involved | Person → Event | `participant_role` 당사자/상대/요청자/목격자 · `feeling` | 김민수→"일정 갈등": 당사자, 미안함 |

## 시간 처리 (bi-temporal)
- 엣지는 `valid_at`(사실이 성립한 시각)과 `invalid_at`(더 이상 유효하지 않게 된 시각)을 가진다.
- 관계가 바뀌면(동기 → 파트장) 옛 Relationship 엣지에 `invalid_at`이 찍히고 새 엣지가 생긴다. 첨삭은 **현재 유효한** 관계를 쓴다.
- "어제", "다음 주 수요일"은 메시지 시각 기준 절대 시각으로 해석한다.

## 검토할 질문 (담당자가 채운다)
- [ ] `relation_type` 7종으로 충분한가? (예: 교수-학생, 거래처)
- [ ] Event에 `deadline` 같은 날짜 속성을 따로 둘까, 아니면 fact 문장에 맡길까?
- [ ] 호칭만 등장하는 언급("팀장님")을 등록된 상대 별칭과 어떻게 연결할까? → `alias_cases.csv`
- [ ] 그룹 대화방(3인 이상)은 범위에 넣나?
