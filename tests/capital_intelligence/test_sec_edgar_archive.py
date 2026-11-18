from datetime import datetime, timezone

import pytest

from aitos.capital_intelligence.sec_edgar import SecEdgarArchiveBackfillSource


class FakeAdapter:
    DATA_URL = "https://data.sec.gov"
    ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/"

    def __init__(self):
        self.ciks = ["0000000001"]
        self.forms = {"3", "4"}
        self.calls = []
        self.archive = {
            "old.json": {
                "accessionNumber": ["a-3", "a-2", "a-1"],
                "form": ["3", "4", "8-K"],
                "filingDate": ["2020-03-01", "2020-02-01", "2020-01-01"],
                "primaryDocument": ["form3.xml", "form4.xml", ""],
                "reportDate": ["", "", ""],
            }
        }

    async def _get_json(self, url):
        self.calls.append(url)
        if url.endswith("/CIK0000000001.json"):
            return {
                "filings": {
                    "files": [
                        {
                            "name": "old.json",
                            "filingFrom": "2020-01-01",
                            "filingTo": "2020-03-01",
                        }
                    ]
                }
            }
        return self.archive[url.rsplit("/", 1)[-1]]

    @staticmethod
    def _day(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)

    async def _information_table_url(self, *args):
        return "https://www.sec.gov/13f.xml"

    async def _parse_13f(self, *args):
        return ["13f"]

    async def _parse_form4(self, cik, accession, filing_date, form, url):
        return [f"{form}:{accession}"]


@pytest.mark.asyncio
async def test_sec_archive_backfill_is_bounded_and_resumes_exactly():
    adapter = FakeAdapter()
    source = SecEdgarArchiveBackfillSource(
        adapter, max_filings_per_poll=1, max_rows_per_poll=10
    )

    first_events, first_cursor = await source.poll()
    assert first_events == ["4:a-2"]
    assert '"row_offset":2' in first_cursor

    second_events, second_cursor = await source.poll(cursor=first_cursor)
    assert second_events == ["3:a-3"]
    assert '"complete":true' in second_cursor


@pytest.mark.asyncio
async def test_sec_archive_backfill_fails_closed_when_cursor_file_disappears():
    source = SecEdgarArchiveBackfillSource(FakeAdapter())
    cursor = '{"0000000001":{"file_name":"missing.json","row_offset":0}}'
    with pytest.raises(RuntimeError, match="file disappeared"):
        await source.poll(cursor=cursor)


@pytest.mark.asyncio
async def test_sec_archive_backfill_rejects_inconsistent_submission_arrays():
    adapter = FakeAdapter()
    adapter.archive["old.json"]["form"].pop()
    source = SecEdgarArchiveBackfillSource(adapter)
    with pytest.raises(RuntimeError, match="inconsistent lengths"):
        await source.poll()
