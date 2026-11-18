# evals/ — 평가셋·루브릭·비교 실험

담당: 평가·품질 팀원 (정답표·루브릭), 팀장 (실행 하니스).
**사례를 쓰는 법(열 설명·예시·흔한 실수)은 [docs/dataset-guide.md](../docs/dataset-guide.md)** — 열 이름은 그 문서가 기준이고 코드가 그 열을 읽는다.

| 파일 | 내용 |
|---|---|
| [characters.csv](characters.csv) | 등장인물 표 (가이드 4단계). `relation`(팀장/동기/…) → `relationship_type`·호칭. 합성 대화의 `contact`와 이름이 같아야 한다 |
| [eval_cases.csv](eval_cases.csv) | 평가 사례. 열: `case_id, relation, situation, original_text, preserve_items, must_not_change, expected_notes, needs_graph, relevant_msg_ids, as_of, split`. 1주차 20 → 2주차 60 → 3주차 120 (관계 4종 × 상황 5종 × 6) |
| [preserve_rules.md](preserve_rules.md) | 보존 항목 분류표와 검증 규칙 — `backend/app/ai/preserve.py`의 근거 |
| [style_rubric.md](style_rubric.md) | 스타일 5점 루브릭 (블라인드 평가·LLM judge 공용) |
| `run_eval.py` | 사례 × 조건(A/B/C/D)을 백엔드 워크플로로 실행해 `results/`에 CSV 저장 |
| `layer_diff.py` | 같은 초안을 모든 상대에게 돌려 층별(문체 예시·프롬프트·결과)로 두 상대가 얼마나 닮았는지 숫자로 냄. `--generate` 없이는 모델 호출 없음. 아래 "층별 비교" 절 |
| `suggestion_accuracy.py` | 제안 정확도: 말투 제안이 정답 설정과 얼마나 맞는지 채점 (문장을 만들지 않음, 모델 호출 없음). 아래 "제안 정확도" 절 |
| `gold_personas.csv` | **정답 설정 타임라인** — 평가 담당이 쓴다 (아직 없음). 형식은 아래 절 |
| [gold_personas.example.csv](gold_personas.example.csv) | 위 파일의 **예시**. 개발자가 형식을 보여 주려고 쓴 것이고 팀의 정답이 아니다 |
| `results/` | 실행 결과. 요약 CSV만 커밋, 원시 jsonl은 제외 |

`preserve_items`의 종류(날짜/시간/수량/금액/사람/행동/가능여부/약속/조건)와 코드의 `PreservedItem.kind` 대응은 `preserve_rules.md`에 적는다.

## 실행

```bash
make eval                                   # = python evals/run_eval.py --conditions A,B
python evals/run_eval.py --split dev --conditions A,B,C --repeat 3 --provider anthropic
```

- 기본 `--provider fake`는 네트워크 없이 파이프라인만 점검한다 (결과 = 원문).
- `--provider anthropic`은 실제 Claude 호출. `.env`의 `LLM_MODEL`을 바꿔 비용·품질을 비교한다.
- `--split dev`로 고치고, `--split test`는 마지막 채점에만 쓴다 (가이드 6단계).
- 같은 사례를 `--repeat 3`으로 돌려 변동을 본다 (project-design §8).
- 형식 검사: `python scripts/validate_data_files.py` (PR마다 CI가 돈다).

## 지표 (project-design §8)
| 지표 | 목표 | 산출 |
|---|---|---|
| 핵심 사실 보존 | 98% 이상, 날짜·금액·약속 변경은 출시 차단 | `validation.passed` 비율 + 사람 검토 (`must_not_change`) |
| 스타일 적합성 | 블라인드 5점 평균 4 이상 | `style_rubric.md` |
| 개인화 효과 | C가 A보다 선호 60% 이상 | 블라인드 쌍대 비교 |
| 그래프 효과 | `needs_graph=Y` 사례에서 D가 C보다 나음 | `relevant_msg_ids` 기준 Recall@5 + 사람 검토 |
| 지연·비용 | p95 8초 목표(실측 후 조정) | `latency_ms`, `token_usage` |

사실 보존 실패는 다른 점수로 상쇄하지 않는다.

## 제안 정확도 (2026-11-16 추가)

우리 서비스는 과거 대화를 보고 "이 상대에게는 이런 말투·호칭이 맞아 보입니다"라고 **설정을 제안**합니다.
이 제안이 맞는지 채점하려면, 사람이 먼저 "이 날짜에 이 상대에게 맞는 설정은 이것"이라고 적어 둔 답안지가
필요합니다. 그 답안지가 `gold_personas.csv`(정답 설정 타임라인)입니다.
배경은 `docs/graph-redesign-2026-11-16.md` 6.1절.

### 1단계: 정답 파일 쓰기 (평가 담당)

1. `gold_personas.example.csv`를 복사해 이름을 `gold_personas.csv`로 바꿉니다. 예시 파일의 내용은 개발자가
   쓴 **예시일 뿐**이니, 합성 대화(`data/synthetic/`)를 직접 읽고 줄을 새로 씁니다.
2. 한 줄은 "이 상대, 이 날짜까지의 대화를 봤을 때 맞는 설정"입니다. 같은 상대를 날짜를 바꿔 여러 줄 씁니다.
   말투가 바뀌는 상대(예: 이서연)는 **바뀌기 전 날짜와 바뀐 뒤 날짜를 둘 다** 넣습니다.
3. 열은 여섯 개입니다. 열 이름은 바꾸지 않습니다.

| 열 | 적는 것 | 예 |
|---|---|---|
| `contact` | 상대 이름 (`characters.csv`의 `name`과 같게) | `이서연` |
| `as_of` | 기준 날짜. 이 날 끝까지의 대화만 본다는 뜻 | `2026-10-21` |
| `speech_level` | 그때 내가 이 상대에게 쓰는 말투. `반말` / `해요체` / `합쇼체` 중 하나. 해요체인지 합쇼체인지 가리기 어려우면 `존댓말`. 반말과 존댓말을 상황에 따라 나눠 쓰거나, 막 바뀌어서 아직 어느 쪽이라고 정하기 이르면 `혼용` — 이때는 프로그램이 값을 정해 주지 않고 **묻는 것**이 정답입니다 | `합쇼체` |
| `address_term` | 그때 내가 부르는 호칭. 대화에 나온 글자 그대로 | `서연 파트장님` |
| `since` | 지금 말투(호칭)로 쓴 **첫 메시지의 날짜**. 처음부터 그대로였으면 비웁니다 | `2026-10-21` |
| `note` | 왜 그렇게 봤는지 메모 (채점에는 안 쓰임) | `c3:m08부터 존댓말` |

4. 칸을 비우는 것과 `-`를 적는 것은 뜻이 다릅니다.
   - **빈칸** = 이 항목은 채점하지 않는다 (판단하기 어렵거나 상관없을 때).
   - **`-`** = "제안이 없어야 맞다". 예: 아직 대화가 없는 날짜의 `speech_level`, 이름을 부른 적이 없는 상대의 `address_term`.
5. `#`으로 시작하는 줄은 설명 줄입니다. 프로그램이 읽지 않으니 파일 맨 위에 메모를 적어도 됩니다.
6. 말투 용어: `합쇼체` = "-습니다/-습니까"로 끝나는 말, `해요체` = "-요"로 끝나는 말, `반말` = 그 밖의 편한 말.
7. 정답을 쓸 때 **프로그램이 어떻게 판단하는지는 생각하지 않습니다.** 대화를 읽은 사람의 눈으로만 씁니다.

### 2단계: 채점 돌리기 (팀장)

```bash
python evals/suggestion_accuracy.py --embedding fake                       # evals/gold_personas.csv 를 채점
python evals/suggestion_accuracy.py --gold evals/gold_personas.example.csv --embedding fake --no-graph
python evals/suggestion_accuracy.py --embedding fake --segment-k 1               # 구간 규칙 K 민감도 (1/2/3, 제품은 2)
python evals/suggestion_accuracy.py --embedding fake --register-min-messages 5   # 메시지 수 문턱을 두면 어떻게 되는지 (제품은 문턱 없음)
```

- 돈이 들지 않습니다. 모델을 부르지 않고, 그래프에 새로 넣지도 않습니다.
- 제안을 만드는 세 가지 방법을 따로 채점해 나란히 보여 줍니다: `register`(메시지의 어미·호칭을 규칙으로 읽는
  새 방법), `graph`(그래프의 관계 사실), `messages`(전체 기간을 한 덩어리로 본 예전 방법).
- `graph`는 그래프에 대화가 이미 들어 있어야 나옵니다(`scripts/import_synthetic.py --user eval --graph`를 한 번
  돌린 뒤 — 이 단계만 비용이 듭니다). 그래프가 꺼져 있으면 표에 "(쓸 수 없음)"으로 나옵니다.
- 결과는 `results/suggestion-accuracy-<시각>.csv`(줄별)와 `-summary.csv`(합계)입니다.

### 3단계: 결과 읽기

줄별 파일의 채점 칸(`boundary` 반말/존댓말, `address_exact` 호칭이 글자까지 같음, `address_contains` 한쪽이
다른 쪽을 포함하면 인정, `since` 바뀐 날짜 ±3일, `level3` 해요체/합쇼체 구분 — register만)에는 이런 값이 들어갑니다.

| 값 | 뜻 |
|---|---|
| `correct` | 제안했고 정답과 맞음 |
| `wrong` | 제안했는데 정답과 다름 |
| `silent_ok` | 제안하지 않았고, 그래도 됨 (정답이 `-`이거나 기본 설정이 이미 정답과 맞음) |
| `missed` | 제안했어야 하는데 하지 않음 |
| `asked` | (`boundary`만) 값을 정해 주지 않고 사용자에게 물었음 — 정답은 한 단계였다. 맞음도 틀림도 아니고 정밀도에서 뺍니다 |
| `no_date` | 제안은 했지만 언제부터인지 말하지 못함 |
| 빈칸 | 정답 파일에서 비워 둔 항목 |

- **제안 정밀도** = `correct` ÷ (`correct` + `wrong`). "제안한 것 중 맞은 비율"입니다. 조용했던 횟수와 물은 횟수(`asked`)는 따로 셉니다.
- **묻는 카드**(`ask` 열이 `Y`): 말투가 바뀐 뒤 메시지가 2건뿐이거나, 최근에 두 말투가 섞여 있거나, 그래프에 "특정 자리에서만"이라는
  단서가 있으면 프로그램은 값을 정해 주지 않고 묻습니다. 정답이 `혼용`이면 `correct`, 정답이 한 단계(`합쇼체` 등)면 `asked`,
  정답이 `-`이면 `wrong`입니다. `pred_speech`에는 카드가 보여 준 단계가 그대로 적힙니다.
- 말투 칸은 그 출처가 **격식을 바꾸자고 했을 때만** 제안으로 셉니다. 호칭만 제안하는 카드에 같이 보이는 "지금 말투"는 제안이 아닙니다
  (세 출처 모두 같은 기준).
- `level3`(합계 표의 "해요/합쇼")은 **정답이 해요체·합쇼체인 줄만**, 제안이 아니라 규칙이 읽은 관찰값(`observed_speech`)으로 채점합니다.
  제안이 없었던 줄(`silent_ok`)도 채점되고, 정답이 반말인 줄은 빈칸입니다.
- `trigger`·`scope_hints` 열은 그래프가 찾은 "말투가 바뀐 계기"와 "범위 단서"입니다. 맞는지는 **사람이 줄마다
  읽고 판정**합니다(자동 채점 없음).
- 모든 제안은 같은 출발점(관계 유형 기본 설정 + `characters.csv`의 호칭)에서 "무엇을 바꾸자고 하는가"로 봅니다.
  그래서 기본 설정이 이미 맞는 상대(예: 팀장 = 존댓말)는 대부분 `silent_ok`가 됩니다.

### 첨삭 비교에 설정 바꿔 넣기 (`run_eval.py`, 팀장)

제안을 실제 첨삭에 적용하면 문장이 어떻게 달라지는지 보는 실험입니다. 새 조건(E, F…)을 만들지 않고 C 조건의
설정만 바꿉니다.

```bash
python evals/run_eval.py --conditions C --persona-override evals/gold_personas.csv        # 정답 설정(상한)
python evals/run_eval.py --conditions C --apply-suggestion all                             # 제안을 전부 적용
python evals/run_eval.py --conditions C --apply-suggestion matching --gold evals/gold_personas.csv  # 정답과 맞는 제안만
```

- `--persona-override`에는 정답 타임라인을 그대로 줄 수 있습니다(`speech_level`을 격식 숫자로 바꿔 넣습니다:
  반말 1, 해요체 3, 합쇼체 4 — 기본 설정이 이미 그 범위면 그대로). 숫자를 직접 정하고 싶으면 `formality` 열을
  더합니다. `contact` 대신 `relation`(팀장/동기) 열로 써도 됩니다.
- 결과 CSV에 열 네 개가 늘었습니다: `validator_version`(보존 규칙 버전 — 이 값이 다른 결과끼리는 수치를 섞어
  비교하지 않습니다), `persona_formality`·`persona_address_term`(그 줄에서 실제로 쓴 설정),
  `suggestion_applied`(무엇을 바꿨는지. 빈칸 = 이 기능을 안 씀, `none` = 바꿀 것이 없었음).
- `--invalidation trust|artifacts|ignore`는 그래프(D)가 "무효가 된 사실"을 어떻게 다룰지 바꿔 보는 실험용입니다.
- 플래그를 하나도 안 쓰면 예전과 똑같이 돕니다(열 네 개만 늘어납니다).

## 실패 신호 열 (`run_eval.py`)

결과 CSV의 `fm*` 열은 `docs/design.md` "실패 유형 점검 (MAST)" 표의 유형 번호를 따른다. 숫자와 참/거짓뿐이고 참고 신호다.

| 열 | 뜻 |
|---|---|
| `fm11_first_checks` | 1차 시도가 걸린 검증 종류 (`fact_added;preserve_missing`). 통과했으면 빈칸 |
| `fm13_attempts` | 생성 시도 횟수 |
| `fm23_length_ratio` | 결과 길이 ÷ 초안 길이 |
| `fm24_no_examples` | C·D인데 프롬프트에 문체 예시가 없었다 |
| `fm25_evidence_unused` | 문체 예시가 있었는데 모델이 근거를 하나도 적지 않았다 (모델이 스스로 적는 값이라 참고만) |
| `fm32_register_warned` | 말투 경고가 붙었다 (`REGISTER_POST_CHECK`를 켠 실행에서만) |
| `fm33_fallback` | 원문 유지로 끝났다 — 표본을 읽어 멀쩡한 결과를 버렸는지 본다 |

`--voice`를 주면 C·D 입력에 말투 지문을 채운다(서비스의 `VOICE_FINGERPRINT`와 같은 값, `--prompt rewrite-v5`와 함께).
주지 않으면 입력은 예전 그대로이고, 그 경우 평가 수치에는 지문의 효과가 들어 있지 않다.

## 층별 비교 (`layer_diff.py`)

"상대가 달라도 결과가 비슷하다"가 어느 층에서 생기는지 좁힌다. 사례의 초안을 등장인물 전원에게 돌리고 상대 쌍마다 잰다.

```bash
python evals/layer_diff.py --embedding fake                               # 조건 C, 모델 호출 없음
python evals/layer_diff.py --conditions B,C --voice --prompt rewrite-v5   # 지문을 켰을 때 프롬프트가 얼마나 갈리는지
python evals/layer_diff.py --voice --prompt rewrite-v5 --generate --provider anthropic --case E001   # 결과 문장까지 (비용)
```

- `style_similarity`: 두 상대의 문체 예시 글이 닮은 정도. `prompt_similarity`: 초안을 뺀 프롬프트. `result_similarity`: 결과 문장(`--generate`).
- 프롬프트 유사도는 낮은데 결과 유사도가 높으면 지시문 쪽을, 프롬프트 유사도부터 높으면 검색·설정·지문 쪽을 본다.
- 글자 유사도는 거칠다(호칭 하나만 달라도 거의 같다). 블라인드 평가를 대신하지 않는다.
- 출력은 `results/layer-diff-<시각>.csv`, 사례 id·관계 이름·숫자뿐이다. `.env`에 `DATABASE_URL`이 있으면 그 DB에 `--user`의 대화를 적재한다.
