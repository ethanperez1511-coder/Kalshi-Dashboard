from __future__ import annotations

from src.modeling.registry import ModelRegistry


def test_registry_returns_category_model():
    registry = ModelRegistry()
    models = registry.get_models_for("Economics")
    categories = [m.category for m in models]
    assert "Economics" in categories


def test_registry_always_includes_fallback():
    registry = ModelRegistry()
    models = registry.get_models_for("UnknownCategory")
    categories = [m.category for m in models]
    assert "fallback" in categories


def test_registry_returns_specialized_plus_fallback():
    registry = ModelRegistry()
    models = registry.get_models_for("Sports")
    categories = [m.category for m in models]
    assert "Sports" in categories
    assert "fallback" in categories


def test_registry_all_models():
    registry = ModelRegistry()
    assert len(registry.all_models) >= 3
