"""Claude API 프로바이더 (anthropic SDK, 구조화 출력).

- `client.messages.parse(..., output_format=ModelOutput)` 로 스키마가 보장된 JSON을 받는다.
- 모델 기본값은 claude-opus-5-5 (설정 LLM_MODEL). 사고 깊이는 `output_config.effort`로 조절한다 (기본 medium).
- 안전 분류기가 거절하면(`stop_reason == "refusal"`) `ModelRefusalError`를 낸다. 워크플로가 재생성 1회를
  시도하고, 그래도 거절이면 원문 유지. 다른 모델로 넘기지는 않는다 (project-design §5.3). 11-08 실측에서
  동생 방 문체 예시가 붙은 평범한 부탁 문장에도 거절이 나왔다.
- 키는 ANTHROPIC_API_KEY 또는 `ant auth login` 프로필에서 SDK가 읽는다. 로그에 프롬프트 원문을 남기지 않는다.
"""

from __future__ import annotations

import logging

import anthropic

from app.ai.providers.base import GenerationResult
from app.core.config import Settings
from app.core.errors import ModelRefusalError, ModelUnavailableError
from app.domain.rewrite import ModelOutput

log = logging.getLogger(__name__)


class AnthropicProvider:
    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key or None,
            timeout=settings.request_timeout_seconds,
            max_retries=1,  # 전체 요청 20초 예산 안에서 재시도는 1회만
        )
        self._model = settings.llm_model
        self._effort = settings.llm_effort
        self._max_tokens = settings.max_output_tokens
        self.model_version = settings.llm_model

    async def generate(self, *, system: str, user: str) -> GenerationResult:
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=ModelOutput,
                output_config={"effort": self._effort},
            )
        except anthropic.RateLimitError as exc:
            raise ModelUnavailableError("모델 요청 한도 초과(429). 잠시 후 다시 시도하세요.") from exc
        except anthropic.APITimeoutError as exc:
            raise ModelUnavailableError("모델 응답 시간 초과.") from exc
        except anthropic.APIStatusError as exc:
            log.warning(
                "anthropic status error %s request_id=%s", exc.status_code, getattr(exc, "request_id", None)
            )
            raise ModelUnavailableError(f"모델 호출 실패 ({exc.status_code}).") from exc
        except anthropic.APIConnectionError as exc:
            raise ModelUnavailableError("모델 서버에 연결할 수 없습니다.") from exc

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            log.info("model refusal category=%s request_id=%s", category, response._request_id)
            raise ModelRefusalError("모델이 요청을 거절했습니다.")
        if response.stop_reason == "max_tokens":
            raise ModelUnavailableError("모델 출력이 길이 한도에서 잘렸습니다.")
        if response.parsed_output is None:
            raise ModelUnavailableError("모델 출력을 스키마로 해석할 수 없습니다.")

        usage = response.usage
        return GenerationResult(
            output=response.parsed_output,
            model_version=response.model,
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            },
        )
