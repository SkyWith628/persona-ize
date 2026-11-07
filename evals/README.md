# evals/ — 평가셋·루브릭·비교 실험

담당: 평가·품질 팀원 (정답표·루브릭), 팀장 (실행 하니스).

| 파일 | 내용 |
|---|---|
| [eval_cases.csv](eval_cases.csv) | 평가 사례: 원문·상황·상대·기대 보존 항목. 1주차 20 → 2주차 60 → 3주차 120 (관계 4종 × 상황 5종 × 6) |
| [preserve_rules.md](preserve_rules.md) | 보존 항목 분류표와 검증 규칙 — `backend/app/ai/preserve.py`의 근거 |
| [style_rubric.md](style_rubric.md) | 스타일 5점 루브릭 (블라인드 평가·LLM judge 공용) |
| `run_eval.py` | 사례 × 조건(A/B/C/D)을 백엔드 워크플로로 실행해 `results/`에 CSV 저장 |
| `results/` | 실행 결과. 요약 CSV만 커밋, 원시 jsonl은 제외 |

## 실행

```bash
make eval                                   # = python evals/run_eval.py --conditions A,B
python evals/run_eval.py --conditions A,B,C --repeat 3 --provider anthropic
```

- 기본 `--provider fake`는 네트워크 없이 파이프라인만 점검한다 (결과 = 원문).
- `--provider anthropic`은 실제 Claude 호출. `.env`의 `LLM_MODEL`을 바꿔 비용·품질을 비교한다.
- 같은 사례를 `--repeat 3`으로 돌려 변동을 본다 (project-design §8).

## 지표 (project-design §8)
| 지표 | 목표 | 산출 |
|---|---|---|
| 핵심 사실 보존 | 98% 이상, 날짜·금액·약속 변경은 출시 차단 | `validation.passed` 비율 + 사람 검토 |
| 스타일 적합성 | 블라인드 5점 평균 4 이상 | `style_rubric.md` |
| 개인화 효과 | C가 A보다 선호 60% 이상 | 블라인드 쌍대 비교 |
| 지연·비용 | p95 8초 목표(실측 후 조정) | `latency_ms`, `token_usage` |

사실 보존 실패는 다른 점수로 상쇄하지 않는다.
