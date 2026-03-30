from chunkhound.code_mapper.models import CodeMapperPOI

import pytest

pytestmark = pytest.mark.unit



def test_code_mapper_poi_carries_mode_and_text() -> None:
    poi = CodeMapperPOI(mode="architectural", text="Core flow")

    assert poi.mode == "architectural"
    assert poi.text == "Core flow"

