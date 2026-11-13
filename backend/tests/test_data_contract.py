"""계약서(§4.3~4.5)의 경로·필드 이름이 그대로 있는지 OpenAPI로 확인한다.

화면 담당이 계약서만 보고 만들기 때문에, 경로나 응답 필드 이름이 바뀌면 여기서 먼저 깨져야 한다.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

CONTRACT_PATHS: list[tuple[str, str]] = [
    # 4.3 상대·페르소나
    ("post", "/v1/contacts"),
    ("get", "/v1/contacts"),
    ("get", "/v1/contacts/{contact_id}"),
    ("patch", "/v1/contacts/{contact_id}"),
    ("delete", "/v1/contacts/{contact_id}"),
    ("put", "/v1/contacts/{contact_id}/persona"),
    ("get", "/v1/contacts/{contact_id}/persona"),
    ("get", "/v1/contacts/{contact_id}/persona/versions"),
    ("get", "/v1/contacts/{contact_id}/persona/suggestion"),
    ("get", "/v1/persona-defaults"),
    # 4.4 대화 가져오기
    ("post", "/v1/imports/parse"),
    ("post", "/v1/conversations"),
    ("get", "/v1/conversations"),
    ("post", "/v1/conversations/{conversation_id}/imports"),
    ("post", "/v1/conversations/{conversation_id}/imports/raw"),
    ("get", "/v1/conversations/{conversation_id}/stats"),
    ("get", "/v1/conversations/{conversation_id}/messages"),
    ("delete", "/v1/conversations/{conversation_id}"),
    ("get", "/v1/imports/{job_id}"),
    ("get", "/v1/data/summary"),
    ("get", "/v1/export"),
    # 4.5 첨삭·사용량
    ("post", "/v1/rewrites"),
    ("get", "/v1/rewrites"),
    ("delete", "/v1/rewrites"),
    ("get", "/v1/rewrites/{request_id}"),
    ("delete", "/v1/rewrites/{request_id}"),
    ("get", "/v1/rewrites/{request_id}/evidence"),
    ("post", "/v1/rewrites/{request_id}/feedback"),
    ("get", "/v1/usage"),
]

# 응답 스키마 → 계약에 적힌 필드 (더 있는 것은 괜찮고, 빠지면 안 된다)
CONTRACT_FIELDS: dict[str, set[str]] = {
    "ImportPreviewOut": {
        "format",
        "variant",
        "title_hint",
        "messages_total",
        "date_from",
        "date_to",
        "speakers",
        "sample",
        "skipped_lines",
        "system_lines",
        "masked_pii",
        "problems",
    },
    "ImportSpeakerOut": {"name", "count", "suggested_role"},
    "ImportSampleOut": {"id", "speaker", "sent_at", "text"},
    "ConversationStatsOut": {
        "conversation_id",
        "messages",
        "my_messages",
        "episodes",
        "graph_enabled",
        "graph_ingested",
        "graph_pending",
        "graph_held",
        "graph_limited",
        "first_message_at",
        "last_message_at",
        "latest_job",
    },
    "ContactSummaryOut": {
        "contact_id",
        "alias",
        "relationship_type",
        "persona_version",
        "conversations",
        "messages",
        "my_messages",
        "graph_ingested",
        "rewrites",
        "last_rewrite_at",
    },
    "DataSummaryOut": {"contacts", "totals", "graph_enabled", "retention_days"},
    "DataTotalsOut": {"contacts", "conversations", "messages", "rewrites"},
    "PersonaSuggestionOut": {"based_on_messages", "settings", "signals"},
    "PersonaSignalOut": {"field", "value", "reason"},
    "RewriteOut": {
        "id",
        "contact_id",
        "draft_version",
        "condition",
        "channel",
        "created_at",
        "result",
        "feedback",
    },
    "FeedbackOut": {"accepted", "edited_text", "reason", "reason_code", "consent_to_reuse", "created_at"},
    "EvidenceOut": {"items", "missing", "refs_recorded"},
    "EvidenceItemOut": {"id", "source_message_ids", "messages"},
    "EvidenceMessageOut": {"speaker_role", "sent_at", "text"},
    "RewritesDeletedOut": {"deleted"},
    "UsageOut": {
        "day",
        "rewrites",
        "rewrite_limit",
        "remaining_rewrites",
        "input_tokens",
        "output_tokens",
        "token_limit",
        "graph_messages_ingested",
        "graph_message_cap",
    },
    "ImportJobOut": {
        "id",
        "conversation_id",
        "status",
        "imported",
        "duplicates",
        "episodes",
        "error_code",
        "error_message",
        "graph_status",
        "graph_episodes",
        "graph_error",
        "created_at",
        "index_done",
        "index_total",
    },
}


def test_openapi_has_every_contract_path_and_field(client: TestClient) -> None:
    spec = client.get("/openapi.json")
    assert spec.status_code == 200, spec.text
    doc = spec.json()
    missing = [f"{m.upper()} {p}" for m, p in CONTRACT_PATHS if m not in doc["paths"].get(p, {})]
    assert missing == []
    schemas = doc["components"]["schemas"]
    for name, fields in CONTRACT_FIELDS.items():
        assert name in schemas, name
        assert fields <= set(schemas[name]["properties"]), (name, fields - set(schemas[name]["properties"]))


def test_request_enums_match_the_contract(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    def enum_of(schema: str, field: str) -> set[str]:
        prop = schemas[schema]["properties"][field]
        options = [prop, *prop.get("anyOf", [])]
        values: set[str] = set()
        for opt in options:
            values |= set(opt.get("enum", []))
            if "const" in opt:
                values.add(opt["const"])
        return values

    assert enum_of("RewriteCreate", "condition") == {"A", "B", "C", "D", "auto"}
    assert enum_of("RewriteCreate", "adjust") == {"shorter", "longer", "more_formal", "more_casual"}
    assert enum_of("RewriteCreate", "channel") == {"web", "mobile", "desktop", "api"}
    assert enum_of("FeedbackIn", "reason_code") == {
        "too_stiff",
        "too_casual",
        "too_servile",
        "not_my_style",
        "fact_changed",
        "too_long",
        "too_short",
        "other",
    }
    assert enum_of("ImportParseIn", "format") == {"auto", "kakao", "telegram", "plain", "json"}
    assert enum_of("ImportJobOut", "graph_status") >= {
        "skipped",
        "pending",
        "running",
        "completed",
        "failed",
        "limited",
    }
    assert enum_of("RewriteOut", "condition") == {"A", "B", "C", "D"}  # 응답은 실제 적용된 조건
    # 기본값: 기존 클라이언트는 아무것도 바꾸지 않아도 된다
    props = schemas["RewriteCreate"]["properties"]
    assert (props["condition"]["default"], props["channel"]["default"]) == ("B", "web")
    assert schemas["ImportParseIn"]["properties"]["tz_offset_minutes"]["default"] == 540
    raw = schemas["ImportRawIn"]["properties"]
    assert {"format", "text", "speaker_map", "tz_offset_minutes", "keep_recent"} <= set(raw)
    assert set(schemas["ContactUpdate"]["properties"]) == {"alias", "relationship_type"}
    assert schemas["ContactCreate"]["properties"]["use_default_persona"]["default"] is False
