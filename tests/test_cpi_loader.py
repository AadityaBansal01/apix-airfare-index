"""CPI loader tests.

The loader is the only route by which an official statistic enters this system,
so it gets the same scrutiny as the index arithmetic. Its job is to accept what a
human actually downloads and to refuse everything else loudly, because a silently
mis-parsed CPI value would corrupt the one chart whose whole purpose is to be
checkable against a published source.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from scripts.load_cpi import DEFAULT_SERIES, load, parse_month

REPO = Path(__file__).resolve().parent.parent


# ==========================================================================
# Month parsing: accept what real exports contain
# ==========================================================================

@pytest.mark.parametrize("raw,expected", [
    ("2026-07", dt.date(2026, 7, 1)),
    ("2026-7", dt.date(2026, 7, 1)),
    ("2026-07-01", dt.date(2026, 7, 1)),
    ("2026-07-23", dt.date(2026, 7, 1)),   # a day is discarded, see below
    ("Jul-2026", dt.date(2026, 7, 1)),
    ("jul-2026", dt.date(2026, 7, 1)),
    ("July 2026", dt.date(2026, 7, 1)),
    ("JULY/2026", dt.date(2026, 7, 1)),
    ("07/2026", dt.date(2026, 7, 1)),
    ("7-2026", dt.date(2026, 7, 1)),
    ("Dec-2024", dt.date(2024, 12, 1)),
])
def test_accepts_the_shapes_real_exports_use(raw, expected):
    assert parse_month(raw) == expected


def test_day_is_always_discarded():
    """A monthly index has no day. Storing one invites a false join against the
    daily series, which is exactly the mistake this chart exists to expose."""
    for raw in ("2026-07-01", "2026-07-15", "2026-07-31"):
        assert parse_month(raw).day == 1


@pytest.mark.parametrize("raw", [
    "", "   ", "nonsense", "2026", "Smarch-2026", "13-2026-xx", "2026/07/01/02",
])
def test_rejects_what_it_cannot_read(raw):
    with pytest.raises(ValueError):
        parse_month(raw)


def test_rejection_message_shows_the_accepted_form():
    with pytest.raises(ValueError, match="2026-07 or Jul-2026"):
        parse_month("wat")


# ==========================================================================
# CSV loading
# ==========================================================================

def _db_available() -> bool:
    try:
        from apix import db
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM apix.cpi_reference LIMIT 1")
            cur.fetchone()
            return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(), reason="postgres not reachable; run `make db-up schema`")


@pytest.fixture
def clean_cpi():
    from apix import db
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM apix.cpi_reference WHERE series_code LIKE 'PYTEST%'")
    yield
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM apix.cpi_reference WHERE series_code LIKE 'PYTEST%'")


def write_csv(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cpi.csv"
    p.write_text(body)
    return p


@needs_db
def test_loads_a_well_formed_export(tmp_path, clean_cpi):
    from apix import db
    csv = write_csv(tmp_path, (
        "month,index_value,sector,base_year,description\n"
        "2026-07,118.4,combined,2024,Transport\n"
        "2026-08,119.1,combined,2024,Transport\n"
    ))
    written, skipped = load(csv, "PYTEST_A", "http://example.test")
    assert (written, skipped) == (2, 0)

    rows = db.fetch_all(
        "SELECT * FROM apix.cpi_reference WHERE series_code='PYTEST_A' "
        "ORDER BY ref_month")
    assert [r["ref_month"] for r in rows] == [dt.date(2026, 7, 1), dt.date(2026, 8, 1)]
    assert float(rows[0]["index_value"]) == 118.4
    assert rows[0]["base_year"] == 2024


@needs_db
def test_alternative_column_names_are_accepted(tmp_path, clean_cpi):
    """Different eSankhyiki exports label the same two columns differently."""
    csv = write_csv(tmp_path, "period,value\n2026-07,118.4\nAug-2026,119.1\n")
    written, _ = load(csv, "PYTEST_B", "http://example.test")
    assert written == 2


@needs_db
def test_defaults_are_applied_when_optional_columns_are_absent(tmp_path, clean_cpi):
    from apix import db
    csv = write_csv(tmp_path, "month,index_value\n2026-07,118.4\n")
    load(csv, "PYTEST_C", "http://example.test")
    row = db.fetch_one(
        "SELECT * FROM apix.cpi_reference WHERE series_code='PYTEST_C'")
    assert row["sector"] == "combined"
    assert row["base_year"] == 2024


@needs_db
def test_thousands_separators_survive(tmp_path, clean_cpi):
    from apix import db
    csv = write_csv(tmp_path, 'month,index_value\n2026-07,"1,184.5"\n')
    load(csv, "PYTEST_D", "http://example.test")
    row = db.fetch_one("SELECT * FROM apix.cpi_reference WHERE series_code='PYTEST_D'")
    assert float(row["index_value"]) == 1184.5


@needs_db
def test_bad_rows_are_skipped_not_guessed(tmp_path, clean_cpi):
    """A row we cannot read is dropped and counted. It is never interpolated:
    an invented CPI value is worse than a gap in the chart."""
    csv = write_csv(tmp_path, (
        "month,index_value\n"
        "2026-07,118.4\n"
        "garbage,119.1\n"        # unparseable month
        "2026-08,notanumber\n"   # unparseable value
        "2026-09,-5\n"           # non-positive index
        "2026-10,120.2\n"
    ))
    written, skipped = load(csv, "PYTEST_E", "http://example.test")
    assert written == 2
    assert skipped == 3


@needs_db
def test_reload_updates_rather_than_duplicating(tmp_path, clean_cpi):
    """A revised CPI release must replace the value, not sit alongside it."""
    from apix import db
    load(write_csv(tmp_path, "month,index_value\n2026-07,118.4\n"),
         "PYTEST_F", "http://example.test")
    load(write_csv(tmp_path, "month,index_value\n2026-07,121.9\n"),
         "PYTEST_F", "http://example.test")

    rows = db.fetch_all(
        "SELECT * FROM apix.cpi_reference WHERE series_code='PYTEST_F'")
    assert len(rows) == 1
    assert float(rows[0]["index_value"]) == 121.9


@needs_db
def test_sectors_are_stored_separately(tmp_path, clean_cpi):
    from apix import db
    csv = write_csv(tmp_path, (
        "month,index_value,sector\n"
        "2026-07,118.4,combined\n"
        "2026-07,117.2,rural\n"
        "2026-07,119.6,urban\n"
    ))
    written, _ = load(csv, "PYTEST_G", "http://example.test")
    assert written == 3
    rows = db.fetch_all(
        "SELECT sector FROM apix.cpi_reference WHERE series_code='PYTEST_G'")
    assert {r["sector"] for r in rows} == {"combined", "rural", "urban"}


@needs_db
def test_unknown_sector_falls_back_to_combined(tmp_path, clean_cpi):
    from apix import db
    csv = write_csv(tmp_path, "month,index_value,sector\n2026-07,118.4,All India\n")
    load(csv, "PYTEST_H", "http://example.test")
    row = db.fetch_one("SELECT * FROM apix.cpi_reference WHERE series_code='PYTEST_H'")
    assert row["sector"] == "combined"


def test_missing_required_columns_is_a_hard_error(tmp_path):
    csv = write_csv(tmp_path, "quarter,amount\n2026-Q1,118.4\n")
    with pytest.raises(SystemExit, match="need a month column"):
        load(csv, "PYTEST_I", "http://example.test")


def test_a_file_with_no_usable_rows_is_a_hard_error(tmp_path):
    csv = write_csv(tmp_path, "month,index_value\nrubbish,rubbish\n")
    with pytest.raises(SystemExit, match="no usable rows"):
        load(csv, "PYTEST_J", "http://example.test")


# ==========================================================================
# The demo must not ship a fabricated official statistic
# ==========================================================================

@needs_db
def test_demo_database_holds_no_cpi_data():
    """The overlay ships empty on purpose.

    MoSPI's endpoints are not reachable programmatically from outside India, so
    there is no real series to load. If this test ever fails, someone has put a
    number in the CPI table and the dashboard is now presenting it next to a
    real index as though it were official. Check where it came from.
    """
    from apix import db

    # Filtered in Python, not with a SQL LIKE: psycopg scans the whole statement
    # for placeholders whenever params are passed, so a bare '%' in the text
    # raises "only '%s', '%b', '%t' are allowed as placeholders". The same
    # footgun is noted in scripts/export_static.py.
    rows = [
        r for r in db.fetch_all(
            "SELECT series_code, count(*) AS n FROM apix.cpi_reference "
            "GROUP BY series_code")
        if not r["series_code"].startswith("PYTEST")
    ]
    assert rows == [], (
        f"unexpected CPI data present: {rows}. Only load a series actually "
        f"exported from eSankhyiki.")


def test_the_only_bundled_fixture_is_labelled_synthetic():
    """A fixture that could be mistaken for real CPI must announce itself."""
    fixtures = list((REPO / "tests/fixtures").glob("*cpi*.csv"))
    for f in fixtures:
        assert "SYNTHETIC" in f.name.upper(), f"{f.name} does not say it is synthetic"
        assert "SYNTHETIC" in f.read_text().upper()
