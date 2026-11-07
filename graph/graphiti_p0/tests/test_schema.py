"""커스텀 타입이 Graphiti 규칙과 맞는지 확인한다 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import ValidationError

from p0.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, Event, Relationship


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
