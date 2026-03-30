from chunkhound.code_mapper.pipeline import _operational_poi_budget

import pytest

pytestmark = pytest.mark.unit



def test_operational_poi_budget_scales_with_comprehensiveness() -> None:
    assert _operational_poi_budget("minimal") == 1
    assert _operational_poi_budget("low") == 2
    assert _operational_poi_budget("medium") == 3
    assert _operational_poi_budget("high") == 4
    assert _operational_poi_budget("ultra") == 5

