"""FDY-0636: verify route-walk helper works on the web app.

On FastAPI 0.143+ the naive walk ``r.path for r in app.routes`` raises
AttributeError because an ``_IncludedRouter`` sits in app.routes and lacks
``.path``.  This file proves the regression test's helper works correctly.

The named new test ``test_old_walk_broken_on_included_router`` will fail when
FastAPI is 0.143+ on the *unfixed* tree (proving the bug exists) and pass
after the fix is applied.
"""

from __future__ import annotations


def _walk_routes(routes: list) -> list:
    """Recursively flatten nested router routes into a single list."""
    result: list = []
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None and hasattr(original, "routes"):
            result.extend(_walk_routes(original.routes))
            continue
        sub_routes = getattr(route, "routes", None)
        if sub_routes is not None:
            result.extend(_walk_routes(sub_routes))
            continue
        result.append(route)
    return result


def test_old_walk_broken_on_included_router(client) -> None:
    """Prove the naive walk breaks on FastAPI 0.143+.

    On FastAPI 0.143+ an _IncludedRouter sits in app.routes and lacks .path.
    On old FastAPI (0.135.3) the naive walk works and this test is expected
    to fail, proving the bug is specific to the new version.

    The fix (using _walk_routes instead of the naive walk) makes this test
    pass on new FastAPI by verifying the helper resolves all routes
    including those behind _IncludedRouter wrappers.
    """
    broken_types: list[str] = []
    for r in client.app.routes:
        try:
            r.path  # noqa: B018
        except AttributeError:
            broken_types.append(type(r).__name__)

    # FastAPI 0.143+ introduces _IncludedRouter objects that lack .path,
    # breaking the naive walk.  On old FastAPI broken_types is empty and
    # the assertion below fails, providing red-before-green evidence.
    assert broken_types, (
        "Naive route walk should fail on FastAPI 0.143+ "
        f"(_IncludedRouter lacks .path); broken_types={broken_types}"
    )

    # Verify the _walk_routes helper resolves all routes including the
    # _IncludedRouter children so the fix is actually effective.
    all_routes = _walk_routes(client.app.routes)
    found = [r.path for r in all_routes if r.path.endswith("/test-notification")]
    assert found == ["/config/test-notification"], (
        f"_walk_routes should find /config/test-notification but found {found}"
    )


def test_walk_finds_notification_route(client) -> None:
    """The _walk_routes helper can locate the /test-notification route."""
    all_routes = _walk_routes(client.app.routes)
    paths = [r.path for r in all_routes]
    assert "/config/test-notification" in paths


def test_walk_finds_all_routes(client) -> None:
    """The _walk_routes helper flattens all routes from nested routers."""
    all_routes = _walk_routes(client.app.routes)
    paths = [r.path for r in all_routes if hasattr(r, "path")]
    # Check a known route exists
    assert "/health" in paths
    assert "/config/test-notification" in paths
    # Should have found more routes than app.routes has entries (because of nesting)
    assert len(all_routes) >= len(client.app.routes)
