from datetime import datetime, timezone

import pytest

from aitos.capital_intelligence.cftc import CftcCotAdapter


FIELD_MAP = {
    "report_date": "report_date_as_yyyy_mm_dd",
    "market_code": "cftc_contract_market_code",
    "market_name": "market_and_exchange_names",
    "open_interest": "open_interest_all",
    "categories": {
        "noncommercial": {
            "long": "noncomm_positions_long_all",
            "short": "noncomm_positions_short_all",
        },
        "commercial": {
            "long": "comm_positions_long_all",
            "short": "comm_positions_short_all",
        },
    },
}


class Response:
    def __init__(self, rows):
        self.rows = rows
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None

    async def json(self):
        return self.rows


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params):
        self.requests.append((url, params))
        return Response(self.responses.pop(0))


@pytest.mark.asyncio
async def test_cftc_cot_ingests_aggregate_positions_and_advances_offset_cursor():
    row = {
        "report_date_as_yyyy_mm_dd": "2026-11-10T00:00:00.000",
        "cftc_contract_market_code": "133741",
        "market_and_exchange_names": "Bitcoin - CME",
        "open_interest_all": "10000",
        "noncomm_positions_long_all": "3000",
        "noncomm_positions_short_all": "2000",
        "comm_positions_long_all": "1000",
        "comm_positions_short_all": "2000",
    }
    session = Session([[row], []])
    adapter = CftcCotAdapter(
        dataset_id="6dca-aqww",
        field_map=FIELD_MAP,
        report_type="legacy_futures_only",
        revision_refresh_pages_per_poll=0,
        session=session,
    )
    events, cursor = await adapter.poll()
    assert len(events) == 1
    event = events[0]
    assert event.source_kind == "cftc_cot_aggregate"
    assert event.instrument == "133741"
    assert event.quantity == 10_000
    assert event.usd_value is None
    assert event.actor_id is None
    assert event.metadata["aggregate_only"] is True
    assert event.metadata["positioning"]["noncommercial"]["net_contracts"] == 1_000
    assert event.metadata["positioning"]["commercial"]["net_contracts"] == -1_000
    assert event.metadata["publication_timestamp_not_supplied"] is True
    assert cursor == ('{"incremental_offset":1,"refresh_offset":0,"version":2}')

    empty_events, same_cursor = await adapter.poll(cursor=cursor)
    assert empty_events == []
    assert same_cursor == cursor
    assert session.requests[1][1]["$offset"] == "1"


def test_cftc_cot_rejects_untrusted_dataset_and_field_identifiers():
    with pytest.raises(ValueError, match="dataset id"):
        CftcCotAdapter(dataset_id="../bad", field_map=FIELD_MAP)
    unsafe = {**FIELD_MAP, "report_date": "date;DROP_TABLE"}
    with pytest.raises(ValueError, match="field mapping"):
        CftcCotAdapter(dataset_id="6dca-aqww", field_map=unsafe)


@pytest.mark.asyncio
async def test_cftc_revision_refresh_replays_historical_rows_idempotently():
    original = {
        "report_date_as_yyyy_mm_dd": "2026-11-10T00:00:00.000",
        "cftc_contract_market_code": "133741",
        "market_and_exchange_names": "Bitcoin - CME",
        "open_interest_all": "10000",
        "noncomm_positions_long_all": "3000",
        "noncomm_positions_short_all": "2000",
        "comm_positions_long_all": "1000",
        "comm_positions_short_all": "2000",
    }
    revised = {**original, "noncomm_positions_long_all": "3500"}
    session = Session(
        [
            [original],  # incremental cursor
            [original],  # historical refresh page 0
            [],  # end refresh cycle
            [],  # incremental cursor now at end
            [revised],  # corrected historical row
            [],  # end refresh cycle
        ]
    )
    adapter = CftcCotAdapter(
        dataset_id="6dca-aqww",
        field_map=FIELD_MAP,
        page_limit=1,
        revision_refresh_pages_per_poll=2,
        session=session,
    )

    first_events, cursor = await adapter.poll()
    assert len(first_events) == 1
    first_long = first_events[0].metadata["positioning"]["noncommercial"][
        "long_contracts"
    ]
    assert first_long == 3000

    revised_events, next_cursor = await adapter.poll(cursor=cursor)
    assert len(revised_events) == 1
    assert revised_events[0].event_id == first_events[0].event_id
    revised_long = revised_events[0].metadata["positioning"]["noncommercial"][
        "long_contracts"
    ]
    assert revised_long == 3500
    assert next_cursor == cursor
    assert [params["$offset"] for _, params in session.requests] == [
        "0",
        "0",
        "1",
        "1",
        "0",
        "1",
    ]


@pytest.mark.asyncio
async def test_cftc_cursor_migrates_legacy_offset_and_validates_revision_cursor():
    adapter = CftcCotAdapter(
        dataset_id="6dca-aqww",
        field_map=FIELD_MAP,
        revision_refresh_pages_per_poll=0,
        session=Session([[]]),
    )
    events, cursor = await adapter.poll(cursor='{"offset":7}')
    assert events == []
    assert cursor == ('{"incremental_offset":7,"refresh_offset":0,"version":2}')
    with pytest.raises(ValueError, match="version-2 cursor"):
        await adapter.poll(
            cursor='{"version":2,"incremental_offset":-1,"refresh_offset":0}'
        )
