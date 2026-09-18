"""Server-side validation of the deterrent randomisation ranges.

Until v1.17 the only limits on these were HTML ``max`` attributes. A
hand-edited scarguard.yml could set pre_delay_range: [300, 300] and produce
several minutes of sprinkler activity from one button press, long after the web
route had given up and told the operator the service was down. These values
drive physical hardware, so they are validated where they are saved.
"""

from __future__ import annotations

import pytest
from config_model import ActuationDefaultsConfig, DeterrentGroupConfig
from deterrent_safety import MAX_ACTUATION_SEC
from pydantic import ValidationError

RANGE_FIELDS = [
    "device_count_range",
    "spray_duration_range",
    "inter_device_delay_range",
    "pre_delay_range",
]


class TestDefaultsRanges:
    def test_defaults_are_valid(self) -> None:
        """The shipped defaults must satisfy the rules they are validated by."""
        ActuationDefaultsConfig()

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_rejects_wrong_length(self, field: str) -> None:
        for bad in ([], [1], [1, 2, 3]):
            with pytest.raises(ValidationError, match="two values"):
                ActuationDefaultsConfig(**{field: bad})

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_rejects_inverted_range(self, field: str) -> None:
        with pytest.raises(ValidationError, match="must not exceed"):
            ActuationDefaultsConfig(**{field: [3, 2]})

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_rejects_negative(self, field: str) -> None:
        with pytest.raises(ValidationError):
            ActuationDefaultsConfig(**{field: [-1, 5]})

    def test_rejects_the_unbounded_pre_delay_that_motivated_this(self) -> None:
        with pytest.raises(ValidationError, match="between"):
            ActuationDefaultsConfig(pre_delay_range=[300.0, 300.0])

    def test_rejects_spray_above_the_actuation_ceiling(self) -> None:
        """A range above the clamp would be silently truncated at fire time."""
        with pytest.raises(ValidationError, match="between"):
            ActuationDefaultsConfig(
                spray_duration_range=[MAX_ACTUATION_SEC + 1, MAX_ACTUATION_SEC + 5],
            )

    def test_accepts_the_ceiling_itself(self) -> None:
        ActuationDefaultsConfig(spray_duration_range=[0.5, MAX_ACTUATION_SEC])

    def test_rejects_absurd_device_count(self) -> None:
        with pytest.raises(ValidationError, match="between"):
            ActuationDefaultsConfig(device_count_range=[1, 500])

    def test_rejects_nan_and_inf(self) -> None:
        for bad in (float("nan"), float("inf")):
            with pytest.raises(ValidationError):
                ActuationDefaultsConfig(spray_duration_range=[1.0, bad])


class TestGroupOverrideRanges:
    """Per-group overrides bypass the defaults, so they need the same rules."""

    def test_none_means_inherit_and_is_allowed(self) -> None:
        g = DeterrentGroupConfig(name="g")
        assert g.pre_delay_range is None

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_override_is_validated(self, field: str) -> None:
        with pytest.raises(ValidationError):
            DeterrentGroupConfig(**{"name": "g", field: [0, 9999]})

    def test_group_override_cannot_smuggle_a_long_pre_delay(self) -> None:
        with pytest.raises(ValidationError, match="between"):
            DeterrentGroupConfig(name="g", pre_delay_range=[300.0, 300.0])
