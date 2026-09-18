"""Server-side validation of the deterrent randomisation ranges.

Until v1.17 the only limits on these were HTML ``max`` attributes. A
hand-edited scarguard.yml could set pre_delay_range: [300, 300] and produce
several minutes of sprinkler activity from one button press, long after the web
route had given up and told the operator the service was down. These values
drive physical hardware, so they are validated where they are saved.
"""

from __future__ import annotations

import pytest
from config_model import (
    ActuationConfig,
    ActuationDefaultsConfig,
    check_actuation_range,
)
from deterrent_safety import MAX_ACTUATION_SEC

RANGE_FIELDS = [
    "device_count_range",
    "spray_duration_range",
    "inter_device_delay_range",
    "pre_delay_range",
]


class TestRangeChecker:
    def test_shipped_defaults_pass(self) -> None:
        """The defaults must satisfy the rules they are checked by."""
        d = ActuationDefaultsConfig()
        for field in RANGE_FIELDS:
            assert check_actuation_range(field, getattr(d, field)) is None

    def test_none_means_inherit_and_passes(self) -> None:
        assert check_actuation_range("pre_delay_range", None) is None

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_rejects_wrong_length(self, field: str) -> None:
        for bad in ([], [1], [1, 2, 3]):
            assert "two values" in (check_actuation_range(field, bad) or "")

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_rejects_inverted_range(self, field: str) -> None:
        assert "must not exceed" in (check_actuation_range(field, [3, 2]) or "")

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_accepts_equal_low_and_high(self, field: str) -> None:
        """A fixed, non-random value is legal: low == high is not inverted."""
        assert check_actuation_range(field, [2, 2]) is None

    @pytest.mark.parametrize("field", RANGE_FIELDS)
    def test_rejects_negative(self, field: str) -> None:
        assert check_actuation_range(field, [-1, 5]) is not None

    def test_rejects_the_unbounded_pre_delay_that_motivated_this(self) -> None:
        assert "between" in (check_actuation_range("pre_delay_range", [300.0, 300.0]) or "")

    def test_rejects_spray_above_the_actuation_ceiling(self) -> None:
        """A range above the clamp would be silently truncated at fire time."""
        err = check_actuation_range(
            "spray_duration_range", [MAX_ACTUATION_SEC + 1, MAX_ACTUATION_SEC + 5],
        )
        assert "between" in (err or "")

    def test_accepts_the_ceiling_itself(self) -> None:
        assert check_actuation_range("spray_duration_range", [0.5, MAX_ACTUATION_SEC]) is None

    def test_rejects_absurd_device_count(self) -> None:
        assert "between" in (check_actuation_range("device_count_range", [1, 500]) or "")

    def test_rejects_nan_and_inf(self) -> None:
        for bad in (float("nan"), float("inf")):
            assert check_actuation_range("spray_duration_range", [1.0, bad]) is not None


class TestLoadPathStaysPermissive:
    """A bad range must never discard a working deterrent configuration.

    routes/config.py substitutes a default ActuationConfig() for the whole
    section when parsing raises, so a pydantic validator here would silently
    turn deterrence off, drop the device registry and the groups, and the next
    unrelated save would persist deterrent.enabled: False.
    """

    def test_out_of_bounds_range_still_loads_the_section(self) -> None:
        cfg = ActuationConfig(
            enabled=True,
            devices=[{"name": "v1", "device_id": "id1", "type": "sprinkler"}],
            groups=[{"name": "g", "devices": ["v1"]}],
            defaults={"pre_delay_range": [300.0, 300.0]},
        )
        assert cfg.enabled is True, "deterrence was silently disabled"
        assert len(cfg.devices) == 1, "device registry was discarded"
        assert len(cfg.groups) == 1, "groups were discarded"
        assert cfg.defaults.pre_delay_range == [300.0, 300.0]



class TestSaveRouteRejectsBadRanges:
    """The checks must run where config is actually written.

    The first version of this feature validated only when the pydantic models
    were constructed directly, which the save route never does: it assembles
    raw dicts and hands them to config_store.save. Every test passed and the
    hole was wide open, reachable from the UI's own save button. These tests
    drive the real route and assert nothing was persisted.
    """

    @pytest.fixture()
    def saved(self, monkeypatch):
        """Capture whatever reaches config_store.save."""
        calls: list[dict] = []
        monkeypatch.setattr("config_store.save", lambda cfg: calls.append(cfg))
        return calls

    def test_rejects_the_pre_delay_that_motivated_this(self, client, saved) -> None:
        resp = client.post(
            "/admin/deterrent",
            json={"defaults": {"pre_delay_range": [300, 300]}},
        )
        assert resp.status_code == 400
        assert "pre_delay_range" in resp.json()["error"]
        assert saved == [], "a rejected range was still written to config"

    def test_rejects_spray_above_the_ceiling(self, client, saved) -> None:
        resp = client.post(
            "/admin/deterrent",
            json={"defaults": {"spray_duration_range": [600, 900]}},
        )
        assert resp.status_code == 400
        assert saved == []

    def test_rejects_a_group_override(self, client, saved) -> None:
        """Group overrides bypass the defaults, so they need checking too."""
        resp = client.post(
            "/admin/deterrent",
            json={"groups": [
                {"name": "g", "devices": [], "inter_device_delay_range": [3000, 3000]},
            ]},
        )
        assert resp.status_code == 400
        assert "group g" in resp.json()["error"]
        assert saved == []

    def test_reports_every_problem_not_just_the_first(self, client, saved) -> None:
        resp = client.post(
            "/admin/deterrent",
            json={"defaults": {
                "pre_delay_range": [300, 300],
                "device_count_range": [1, 999],
            }},
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert "pre_delay_range" in err and "device_count_range" in err

    def test_accepts_values_within_bounds(self, client, saved) -> None:
        resp = client.post(
            "/admin/deterrent",
            json={"defaults": {
                "pre_delay_range": [0, 3],
                "spray_duration_range": [3, 8],
                "inter_device_delay_range": [1, 5],
                "device_count_range": [1, 4],
            }},
        )
        assert resp.status_code == 200
        assert len(saved) == 1


class TestTimeoutDerivation:
    """The route wait must outlast the deterrent side's real worst case.

    Hardcoding it drifted twice: 90s against a 120s wait, then a 180s bound
    against a 180s wait with 10ms of margin. Deriving it is only worth
    anything if the derivation itself is pinned.
    """

    def test_timeout_exceeds_the_worst_case_sequence(self) -> None:
        from deterrent_safety import (
            MAX_ACTUATION_SEC,
            MAX_GROUP_TEST_FIRE_SEC,
            MAX_INTER_DELAY_SEC,
            MAX_PRE_DELAY_SEC,
            group_test_fire_timeout_sec,
        )

        # Pre-delay, then the firing window, then one final spray that always
        # runs to completion, then the inter-device wait the loop performs
        # before it notices the window closed.
        worst = (
            MAX_PRE_DELAY_SEC
            + MAX_GROUP_TEST_FIRE_SEC
            + MAX_ACTUATION_SEC
            + MAX_INTER_DELAY_SEC
        )
        assert group_test_fire_timeout_sec() > worst, (
            "the route gives up while hardware may still be firing"
        )

    def test_every_term_is_actually_enforced(self) -> None:
        """A term that nothing clamps makes the derivation fiction."""
        src = (
            __import__("pathlib").Path(__file__).resolve()
            .parents[3] / "services" / "deterrent" / "src" / "group_fire.py"
        ).read_text()
        for name in ("MAX_PRE_DELAY_SEC", "MAX_INTER_DELAY_SEC", "MAX_ACTUATION_SEC"):
            assert name in src, f"{name} is a term in the bound but is clamped nowhere"
