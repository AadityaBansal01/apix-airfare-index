"""API tests.

Two things are load-bearing here and get the most attention.

**No index value escapes without its provenance.** A consumer at the NSO must
never receive a bare number they could mistake for a complete, live measurement.
There is a test asserting every index-bearing endpoint carries the coverage and
seeded flags.

**The API cannot write.** It reads a series the scraper and index engine produce.
A test asserts the read-only guard rejects anything that is not a SELECT.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from apix.api.deps import read_all
from apix.api.main import app


#: The API tests assert against the full demo dataset: six routes, five windows,
#: all three frequencies. Other integration modules truncate and reseed narrower
#: slices for their own purposes, so this module cannot assume it inherits a
#: usable database. It checks the shape it needs and rebuilds if that shape is
#: absent, which also makes the module runnable on its own.
DEMO_START = "2026-07-01"
DEMO_END = "2026-09-04"
EXPECTED_ROUTES = 6
EXPECTED_WINDOWS = 5


def _db_reachable() -> bool:
    try:
        read_all("SELECT 1 AS x")
        return True
    except Exception:
        return False


def _demo_dataset_present() -> bool:
    try:
        shape = read_all(
            """
            SELECT (SELECT count(DISTINCT route_id) FROM apix.apix_route_index
                    WHERE frequency='daily')                       AS routes,
                   (SELECT count(DISTINCT window_days)
                      FROM apix.elementary_index)                  AS windows,
                   (SELECT count(DISTINCT frequency)
                      FROM apix.apix_index)                        AS freqs
            """)[0]
        return (shape["routes"] >= EXPECTED_ROUTES
                and shape["windows"] >= EXPECTED_WINDOWS
                and shape["freqs"] >= 3)
    except Exception:
        return False


def _rebuild_demo_dataset() -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    run = lambda *a: subprocess.run(
        [sys.executable, "-m", "apix.cli", *a], cwd=root,
        capture_output=True, text=True, timeout=300)

    subprocess.run([sys.executable, "scripts/load_reference.py"], cwd=root,
                   capture_output=True, text=True, timeout=120)
    run("seed", "--start", DEMO_START, "--end", DEMO_END,
        "--surge-start", "2026-08-20", "--surge-days", "8", "--surge-peak", "1.45")
    run("base")
    run("index")


DB = _db_reachable()
needs_db = pytest.mark.skipif(
    not DB, reason="postgres not reachable; run `make db-up schema reference`")


@pytest.fixture(scope="module", autouse=True)
def demo_dataset():
    """Guarantee the full demo dataset before any API assertion runs."""
    if DB and not _demo_dataset_present():
        _rebuild_demo_dataset()
    yield


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ==========================================================================
# Service
# ==========================================================================

def test_root_lists_endpoints(client):
    d = client.get("/").json()
    assert d["problem_statement"] == "SIH26056, MoSPI"
    for key in ("latest", "series", "methodology", "compliance"):
        assert key in d["endpoints"]


def test_openapi_document_is_generated(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"].startswith("APIx")
    for path in ("/v1/index/latest", "/v1/index/series", "/v1/methodology",
                 "/v1/compliance", "/v1/leadtime", "/v1/basket"):
        assert path in spec["paths"], f"{path} missing from OpenAPI"


def test_docs_page_renders(client):
    assert client.get("/docs").status_code == 200


def test_health_reports_database_and_seed_state(client):
    d = client.get("/health").json()
    assert d["status"] in ("ok", "degraded")
    assert "seeded_mode" in d
    assert d["version"]


# ==========================================================================
# The read-only guard
# ==========================================================================

def test_read_all_refuses_writes():
    """The API must not be able to mutate the published series, even by bug."""
    for sql in ("DELETE FROM apix.fare",
                "UPDATE apix.apix_index SET index_value = 1",
                "INSERT INTO apix.fare (total_fare) VALUES (1)",
                "DROP TABLE apix.fare",
                "TRUNCATE apix.fare"):
        with pytest.raises(ValueError, match="read-only"):
            read_all(sql)


@needs_db
def test_read_all_permits_select_and_cte():
    """Needs a database: the guard rejects non-SELECT before touching the
    connection, but confirming a SELECT is *permitted* requires one."""
    read_all("SELECT 1 AS x")
    read_all("WITH t AS (SELECT 1 AS x) SELECT * FROM t")


def test_only_get_is_exposed(client):
    """Every route is a GET. A read-only API should not advertise otherwise."""
    for route in app.routes:
        methods = getattr(route, "methods", set())
        assert methods <= {"GET", "HEAD"}, f"{route.path} exposes {methods}"


# ==========================================================================
# Provenance: the rule that no number travels alone
# ==========================================================================

@needs_db
@pytest.mark.parametrize("path", [
    "/v1/index/latest",
    "/v1/index/series?frequency=daily",
    "/v1/index/routes",
])
def test_every_index_response_carries_provenance(client, path):
    d = client.get(path).json()
    p = d["provenance"]
    assert p["formula_elementary"] == "jevons"
    assert p["formula_upper"] == "young_modified_laspeyres"
    assert "= 100" in p["index_reference"]
    assert isinstance(p["is_seeded"], bool)
    assert p["coverage_note"]


@needs_db
def test_coverage_note_names_the_excluded_carriers(client):
    """The 8% coverage figure must be impossible to miss."""
    note = client.get("/v1/index/latest").json()["provenance"]["coverage_note"]
    assert "IndiGo" in note
    assert "minority of the market" in note


@needs_db
def test_seeded_flag_is_derived_from_data_not_configuration(client, monkeypatch):
    """A badge that depends on an env var is one that eventually goes missing."""
    import apix.api.deps as deps
    monkeypatch.setenv("APIX_USE_SEED_DATA", "false")
    deps.basket_config.cache_clear()
    d = client.get("/v1/index/latest").json()
    rows = read_all(
        "SELECT EXISTS (SELECT 1 FROM apix.fare f JOIN apix.source s "
        "USING (source_id) WHERE s.code='seed') AS seeded")
    assert d["provenance"]["is_seeded"] is rows[0]["seeded"]


@needs_db
def test_methodology_flags_seeded_data_as_a_limitation(client):
    m = client.get("/v1/methodology").json()
    seeded = client.get("/health").json()["seeded_mode"]
    mentions = any("SYNTHETIC" in lim for lim in m["limitations"])
    assert mentions is seeded


# ==========================================================================
# Index endpoints
# ==========================================================================

@needs_db
def test_latest_returns_a_usable_point(client):
    d = client.get("/v1/index/latest").json()
    i = d["index"]
    assert float(i["index_value"]) > 0
    assert i["n_cells"] > 0
    assert 0 < float(i["coverage_ratio"]) <= 1


@needs_db
def test_series_is_ordered_and_bounded(client):
    d = client.get("/v1/index/series?frequency=daily").json()
    dates = [p["date"] for p in d["points"]]
    assert dates == sorted(dates)
    assert d["count"] == len(d["points"])
    assert d["start"] == dates[0] and d["end"] == dates[-1]


@needs_db
def test_series_respects_a_date_window(client):
    full = client.get("/v1/index/series?frequency=daily").json()
    mid = full["points"][len(full["points"]) // 2]["date"]
    d = client.get(f"/v1/index/series?frequency=daily&start={mid}").json()
    assert all(p["date"] >= mid for p in d["points"])
    assert d["count"] < full["count"]


@needs_db
def test_series_limit_is_enforced(client):
    d = client.get("/v1/index/series?frequency=daily&limit=5").json()
    assert d["count"] <= 5
    assert client.get("/v1/index/series?limit=99999").status_code == 422


@needs_db
def test_all_three_frequencies_are_built(client):
    for freq in ("daily", "weekly", "monthly"):
        d = client.get(f"/v1/index/series?frequency={freq}").json()
        assert d["count"] > 0, f"{freq} series is empty"


@needs_db
def test_route_contributions_sum_to_the_headline(client):
    """The breakdown must reconstruct the national figure exactly."""
    d = client.get("/v1/index/routes").json()
    total = sum(float(r["contribution"]) for r in d["routes"])
    assert abs(total - float(d["headline"])) < 0.001


@needs_db
def test_route_weights_sum_to_one(client):
    d = client.get("/v1/index/routes").json()
    assert abs(sum(float(r["weight"]) for r in d["routes"]) - 1.0) < 0.001


# ==========================================================================
# Lead time
# ==========================================================================

@needs_db
def test_leadtime_curve_rises_as_departure_nears(client):
    """The defining shape of the artifact: nearer booking costs more."""
    d = client.get("/v1/leadtime?route=BOM-DEL").json()
    pts = sorted(d["points"], key=lambda p: p["window_days"])
    fares = [float(p["mean_fare"]) for p in pts]
    assert fares == sorted(fares, reverse=True), "fares should fall with lead time"
    assert float(pts[0]["ratio_to_longest_window"]) > 1.2
    assert float(pts[-1]["ratio_to_longest_window"]) == pytest.approx(1.0, abs=1e-6)


@needs_db
def test_leadtime_differs_by_route(client):
    """Business-heavy trunk sectors should price late booking harder than
    short-haul leisure ones. Identical curves would mean the generator or the
    query had flattened a real distinction."""
    def premium(route: str) -> float:
        pts = client.get(f"/v1/leadtime?route={route}").json()["points"]
        first = min(pts, key=lambda p: p["window_days"])
        return float(first["ratio_to_longest_window"])

    assert premium("BOM-DEL") > premium("BLR-HYD")


@needs_db
def test_leadtime_route_is_case_insensitive(client):
    assert client.get("/v1/leadtime?route=bom-del").status_code == 200


@needs_db
def test_unknown_route_lists_the_valid_ones(client):
    """Needs a database: without one the endpoint correctly returns 503 rather
    than 404, since it cannot know which routes exist."""
    r = client.get("/v1/leadtime?route=XXX-YYY")
    assert r.status_code == 404
    assert "Available" in r.json()["detail"]


# ==========================================================================
# Reference
# ==========================================================================

@needs_db
def test_basket_weights_sum_to_one(client):
    d = client.get("/v1/basket").json()
    assert abs(float(d["total_weight"]) - 1.0) < 1e-6
    assert len(d["advance_windows"]) == 5


@needs_db
def test_methodology_states_both_formulas_and_cpi_alignment(client):
    m = client.get("/v1/methodology").json()
    assert "Jevons" in m["elementary_formula"]
    assert "Young" in m["upper_formula"]
    assert m["cpi_alignment"]["elementary_formula_matches"] is True
    assert m["cpi_alignment"]["classification"].startswith("COICOP 2018")
    assert m["cpi_alignment"]["division_07_weight_combined"] == 8.796
    assert r"\prod" in m["elementary_formula_latex"]
    assert len(m["limitations"]) >= 6


@needs_db
def test_methodology_documents_the_missing_data_policy(client):
    p = client.get("/v1/methodology").json()["missing_data_policy"]
    assert "Carry forward" in p["cell_missing_under_3_days"]
    assert "renormalise" in p["cell_missing_3_days_or_more"]
    assert "not a price" in p["sold_out"]


# ==========================================================================
# Compliance
# ==========================================================================

@needs_db
def test_compliance_separates_exclusion_from_pending_work(client):
    """Conflating "we chose not to" with "not built yet" would overstate the
    compliance posture. They are different lists."""
    d = client.get("/v1/compliance").json()
    excluded = {e["code"] for e in d["excluded_on_compliance_grounds"]}
    pending = {e["code"] for e in d["pending_implementation"]}

    assert {"indigo", "makemytrip", "goibibo", "airindia"} <= excluded
    assert not excluded & pending
    # A source excluded on compliance grounds must never appear as merely
    # pending: that would read as "we will get to it", which is the opposite of
    # a deliberate refusal.
    assert not (excluded & {"akasa", "spicejet"})
    # Both tier-1 sources now have adapters, so nothing should be pending. The
    # list must still exist and stay empty rather than being removed, because a
    # future adapter belongs in it.
    assert pending == set(), f"unexpected pending sources: {pending}"
    # Internal bookkeeping rows belong in neither list.
    assert "__robots__" not in excluded | pending
    assert "seed" not in excluded | pending


@needs_db
def test_every_excluded_source_states_a_reason(client):
    d = client.get("/v1/compliance").json()
    for e in d["excluded_on_compliance_grounds"]:
        assert e["exclusion_reason"], f"{e['code']} excluded with no reason"
        assert e["tier"] >= 3
        assert e["audit_verdict"] == "do_not_scrape"


@needs_db
def test_compliance_reports_the_request_log(client):
    d = client.get("/v1/compliance").json()
    assert d["sources"]
    for row in d["sources"]:
        assert row["requests"] >= row["requests_sent"]
        assert row["requests"] >= row["robots_blocked"]
