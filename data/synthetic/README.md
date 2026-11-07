# data/synthetic — 합성 과거 대화

담당: 사용자·시나리오 팀원. 그래프 담당이 "사건 연결이 필요한 장면" 요청을 추가한다.
**실존 인물·실제 대화를 넣지 않는다.** 이 데이터는 외부 서비스(Claude API, Neo4j)에 전송된다.

## 형식 (`*.json`)

```json
{
  "_note": "데이터셋 설명과 점검 포인트",
  "user": {"name": "김민수"},
  "conversations": [
    {
      "conversation_id": "c1",
      "title": "박지현 팀장님과의 업무 대화",
      "contact": "박지현",
      "relationship_type": "superior",
      "messages": [
        {"id": "m01", "speaker": "me", "sent_at": "2026-10-05T09:10:00+09:00", "text": "..."},
        {"id": "m02", "speaker": "contact", "sent_at": "2026-10-05T09:25:00+09:00", "text": "..."}
      ]
    }
  ],
  "checks": {"people": [{"canonical": "박지현", "match": "지현"}], "expect_invalidated_edges": true}
}
```

규칙
- `speaker`는 `me`(사용자 본인) 또는 `contact`만. 본인 발화만 문체 예시로 쓰이므로 **사용자의 말투가 상대별로 다르게** 드러나야 한다.
- `sent_at`은 시간대 포함 ISO 8601 (`+09:00`). 시간 순서가 그래프의 사실 무효화에 쓰인다.
- `id`는 대화방 안에서 유일. 다른 대화방과는 겹쳐도 된다.
- `relationship_type`은 `backend/app/domain/persona.py`의 값 중 하나 (superior/colleague/subordinate/friend/family/partner/acquaintance).

검증: `python scripts/validate_data_files.py` (CI에서도 돈다).

## 파일
| 파일 | 내용 |
|---|---|
| `sample_conversations.json` | 36개 메시지, 4개 대화방. 호칭 병합·관계 변화·사건 상태 변화·상대 시간 장치 포함 |

1주차 목표: 사례 20개(평가셋용 원문·상황)와 그에 맞는 과거 대화 추가. 열 이름은 `evals/eval_cases.csv`를 따른다.
