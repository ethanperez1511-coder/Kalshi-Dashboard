from src.modeling.base import ModelResult


def test_model_result_creation():
    result = ModelResult(market_id="FED-RATE-JUL", p_model=0.77, confidence=0.85,
                         reasoning="Historical base rate", data_sources=["FRED", "Fed minutes"])
    assert result.market_id == "FED-RATE-JUL"
    assert result.p_model == 0.77
    assert result.confidence == 0.85
    assert len(result.data_sources) == 2


def test_model_result_clamps_probability():
    result = ModelResult(market_id="X", p_model=1.5, confidence=0.5, reasoning="test")
    assert result.p_model == 1.0
    result2 = ModelResult(market_id="X", p_model=-0.1, confidence=0.5, reasoning="test")
    assert result2.p_model == 0.0
