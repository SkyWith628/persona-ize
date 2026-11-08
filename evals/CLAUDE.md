# evals/ — 평가·품질 담당 폴더

이 폴더에서 Claude와 일할 때 지킬 것. 전체 규칙은 저장소 루트 `CLAUDE.md`, 역할 설명은 `docs/roles/evaluation.md`.

- 여기 파일은 **데이터와 문서**다: `eval_cases.csv`(사례), `characters.csv`(등장인물), `preserve_rules.md`(보존 규칙), `style_rubric.md`(루브릭), `results/`(실행 결과). 코드는 `run_eval.py` 하나이고 팀장이 관리한다 — 고치지 말고 제안한다.
- CSV 열 이름은 `docs/dataset-guide.md`가 기준. **열을 추가·변경하지 않는다**(코드가 읽는다). 값은 가이드의 예시 형식(`날짜=내일 오전;가능여부=금요일 불가`)을 따른다.
- 사례를 쓰거나 고친 뒤에는 저장소 루트에서 `python scripts/validate_data_files.py`를 돌려 통과를 확인한다. UTF-8로 저장한다.
- **실제 대화·실존 인물을 넣지 않는다.** 합성 데이터만.
- `results/*.csv`와 `*.md`는 읽기 전용 기록이다. 결과를 해석할 때는 `violations`·`first_attempt_violations`·`recall`·`warnings` 열을 본다. 실제 모델 호출(`--provider anthropic`)은 비용이 드니 팀장이 돌린다.
- 사실 보존 실패는 다른 점수로 상쇄하지 않는다는 원칙을 루브릭·사례 기대에 그대로 반영한다.
