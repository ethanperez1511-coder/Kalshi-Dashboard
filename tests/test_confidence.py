from src.modeling.confidence import ConfidenceScore


def test_high_confidence():
    score = ConfidenceScore(data_freshness=0.9, data_completeness=0.8, historical_calibration=0.85)
    assert score.overall >= 0.7
    assert score.tier == "high"


def test_medium_confidence():
    score = ConfidenceScore(data_freshness=0.6, data_completeness=0.5, historical_calibration=0.5)
    assert 0.4 <= score.overall < 0.7
    assert score.tier == "medium"


def test_low_confidence():
    score = ConfidenceScore(data_freshness=0.2, data_completeness=0.3, historical_calibration=0.2)
    assert score.overall < 0.4
    assert score.tier == "low"


def test_edge_threshold_by_tier():
    high = ConfidenceScore(data_freshness=0.9, data_completeness=0.9, historical_calibration=0.9)
    medium = ConfidenceScore(data_freshness=0.5, data_completeness=0.5, historical_calibration=0.5)
    low = ConfidenceScore(data_freshness=0.1, data_completeness=0.1, historical_calibration=0.1)
    assert high.edge_threshold == 0.05
    assert medium.edge_threshold == 0.08
    assert low.edge_threshold == 0.12
