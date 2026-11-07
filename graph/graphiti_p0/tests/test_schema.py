"""커스텀 타입이 Graphiti 규칙과 맞는지 확인한다 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import ValidationError

from p0.schema import (
    EDGE_TYPE_MAP,
    EDGE_TYPES,
    ENTITY_TYPES,
    EXTRACTION_INSTRUCTIONS,
    Event,
    Involved,
    Relationship,
)


def test_edge_type_map_refers_to_defined_types() -> None:
    for (source, target), edge_names in EDGE_TYPE_MAP.items():
        assert source in ENTITY_TYPES
        assert target in ENTITY_TYPES
        assert all(name in EDGE_TYPES for name in edge_names)


@pytest.mark.parametrize("model", [*ENTITY_TYPES.values()])
def test_entity_fields_do_not_shadow_reserved_fields(model: type) -> None:
    assert not set(model.model_fields) & set(EntityNode.model_fields)


@pytest.mark.parametrize("model", [*EDGE_TYPES.values()])
def test_edge_fields_do_not_shadow_reserved_fields(model: type) -> None:
    assert not set(model.model_fields) & set(EntityEdge.model_fields)


@pytest.mark.parametrize("model", [*ENTITY_TYPES.values(), *EDGE_TYPES.values()])
def test_all_fields_optional(model: type) -> None:
    # 대화에서 확인되지 않은 속성은 비워 둘 수 있어야 한다 (추측 방지).
    model()


def test_range_validation() -> None:
    with pytest.raises(ValidationError):
        Relationship(closeness=6)
    with pytest.raises(ValidationError):
        Event(importance=0)


def test_participant_role_is_closed_set() -> None:
    # 11-08 1차 수집에서 '참여자', 'requester' 등 값이 섞여 Literal로 닫았다.
    assert Involved(participant_role="요청자").participant_role == "요청자"
    with pytest.raises(ValidationError):
        Involved(participant_role="requester")


@pytest.mark.parametrize(
    "keyword",
    [
        "직접 부른 말",
        "relation_type은 하나",
        "취소·거절·불참",
        "UTC(Z)",
        "엔티티로 만들지 않",
        "Relationship, 사람↔사건이면 Involved만",
    ],
)
def test_instructions_cover_known_failures(keyword: str) -> None:
    # graph/graph-spike-report.md 1차 품질 과제 5건이 지시문에 남아 있어야 한다.
    assert keyword in EXTRACTION_INSTRUCTIONS
