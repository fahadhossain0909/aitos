import json
import pytest

from aitos.capital_intelligence.sec_edgar import SecEdgarFilingsAdapter


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None

    async def text(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.closed = False
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected SEC URL: {url}")
        body = self.responses[url]
        return FakeResponse(body if isinstance(body, str) else json.dumps(body))

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_sec_13f_preserves_report_period_and_filing_publication_time():
    cik = "0000320193"
    accession = "0000320193-26-000001"
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    directory = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/"
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": ["13F-HR"],
                "filingDate": ["2026-05-15"],
                "primaryDocument": ["primary.htm"],
                "reportDate": ["2026-03-31"],
            }
        }
    }
    index = {"directory": {"item": [{"name": "infotable.xml"}]}}
    xml = """<informationTable><infoTable>
      <nameOfIssuer>Example Corp</nameOfIssuer><titleOfClass>COM</titleOfClass>
      <cusip>123456789</cusip><value>2500</value>
      <sshPrnamt>10000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>
      <investmentDiscretion>SOLE</investmentDiscretion>
    </infoTable></informationTable>"""
    session = FakeSession(
        {
            submissions_url: submissions,
            directory + "index.json": index,
            directory + "infotable.xml": xml,
        }
    )
    adapter = SecEdgarFilingsAdapter(
        ciks=[cik], user_agent="AITOS research contact@example.org", session=session
    )
    events, cursor = await adapter.poll()
    assert len(events) == 1
    event = events[0]
    assert event.instrument == "123456789"
    assert event.quantity == 10_000
    assert event.usd_value == 2_500_000
    assert event.event_time.date().isoformat() == "2026-03-31"
    assert event.observed_time.date().isoformat() == "2026-05-15"
    assert (
        event.metadata["source_semantics"]
        == "quarterly_disclosed_holding_not_realtime_position"
    )
    assert json.loads(cursor)[cik] == accession


@pytest.mark.asyncio
async def test_sec_form4_emits_reported_transaction_with_direction_and_lag():
    cik = "0000789019"
    accession = "0000789019-26-000002"
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    document_url = "https://www.sec.gov/Archives/edgar/data/789019/000078901926000002/ownership.xml"
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": ["4"],
                "filingDate": ["2026-10-03"],
                "primaryDocument": ["ownership.xml"],
                "reportDate": ["2026-10-01"],
            }
        }
    }
    xml = """<ownershipDocument>
      <issuer><issuerCik>789019</issuerCik><issuerTradingSymbol>MSFT</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>1234567</rptOwnerCik></reportingOwnerId></reportingOwner>
      <nonDerivativeTable><nonDerivativeTransaction>
        <transactionDate><value>2026-10-01</value></transactionDate>
        <transactionCode><value>A</value></transactionCode>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>400</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </nonDerivativeTransaction></nonDerivativeTable>
    </ownershipDocument>"""
    session = FakeSession({submissions_url: submissions, document_url: xml})
    adapter = SecEdgarFilingsAdapter(
        ciks=[cik], user_agent="AITOS research contact@example.org", session=session
    )
    events, _ = await adapter.poll()
    assert len(events) == 1
    event = events[0]
    assert event.instrument == "MSFT"
    assert event.actor_id == "sec-cik:1234567"
    assert event.counterparty_id == "sec-cik:789019"
    assert event.usd_value == 40_000
    assert event.metadata["flow_sign"] == 1
    assert event.event_time.date().isoformat() == "2026-10-01"
    assert event.observed_time.date().isoformat() == "2026-10-03"


def test_sec_adapter_requires_contactable_user_agent():
    with pytest.raises(ValueError, match="User-Agent"):
        SecEdgarFilingsAdapter(ciks=["123"], user_agent="AITOS research")


@pytest.mark.asyncio
async def test_sec_form3_is_a_point_in_time_holding_not_a_trade():
    cik = "0000789019"
    accession = "0000789019-26-000003"
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    document_url = (
        "https://www.sec.gov/Archives/edgar/data/789019/000078901926000003/form3.xml"
    )
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": ["3"],
                "filingDate": ["2026-09-02"],
                "primaryDocument": ["form3.xml"],
                "reportDate": ["2026-08-31"],
            }
        }
    }
    xml = """<ownershipDocument>
      <periodOfReport>2026-08-31</periodOfReport>
      <issuer><issuerCik>789019</issuerCik><issuerTradingSymbol>MSFT</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>1234567</rptOwnerCik></reportingOwnerId></reportingOwner>
      <nonDerivativeTable><nonDerivativeHolding>
        <securityTitle><value>Common Stock</value></securityTitle>
        <sharesOwnedFollowingTransaction><value>500</value></sharesOwnedFollowingTransaction>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </nonDerivativeHolding></nonDerivativeTable>
    </ownershipDocument>"""
    session = FakeSession({submissions_url: submissions, document_url: xml})
    adapter = SecEdgarFilingsAdapter(
        ciks=[cik], user_agent="AITOS research contact@example.org", session=session
    )
    events, _ = await adapter.poll()
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "insider_holding_disclosure"
    assert event.quantity == 500
    assert event.usd_value is None
    assert event.metadata["flow_sign"] == 0
    assert event.event_time.date().isoformat() == "2026-08-31"
    assert event.observed_time.date().isoformat() == "2026-09-02"


@pytest.mark.asyncio
async def test_sec_filing_batch_limit_advances_cursor_without_skipping_filings():
    cik = "0000789019"
    older = "0000789019-26-000010"
    newer = "0000789019-26-000011"
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    base = "https://www.sec.gov/Archives/edgar/data/789019/"
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [newer, older],
                "form": ["4", "4"],
                "filingDate": ["2026-10-03", "2026-10-02"],
                "primaryDocument": ["new.xml", "old.xml"],
                "reportDate": ["2026-10-01", "2026-09-30"],
            }
        }
    }
    xml = """<ownershipDocument>
      <issuer><issuerCik>789019</issuerCik><issuerTradingSymbol>MSFT</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>1234567</rptOwnerCik></reportingOwnerId></reportingOwner>
      <nonDerivativeTable><nonDerivativeTransaction>
        <transactionDate><value>2026-10-01</value></transactionDate>
        <transactionCode><value>A</value></transactionCode>
        <transactionShares><value>1</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </nonDerivativeTransaction></nonDerivativeTable>
    </ownershipDocument>"""
    session = FakeSession(
        {
            submissions_url: submissions,
            base + "000078901926000010/old.xml": xml,
            base + "000078901926000011/new.xml": xml,
        }
    )
    adapter = SecEdgarFilingsAdapter(
        ciks=[cik],
        user_agent="AITOS research contact@example.org",
        max_filings_per_poll=1,
        initial_filings_per_cik=2,
        session=session,
    )
    first_events, first_cursor = await adapter.poll()
    assert len(first_events) == 1
    assert first_events[0].transaction_id == older
    assert json.loads(first_cursor)[cik] == older

    second_events, second_cursor = await adapter.poll(cursor=first_cursor)
    assert len(second_events) == 1
    assert second_events[0].transaction_id == newer
    assert json.loads(second_cursor)[cik] == newer



@pytest.mark.asyncio
async def test_sec_recent_revision_rescan_reloads_known_accession_without_cursor_regression(
):
    cik = "0000320193"
    accession = "0000320193-26-000001"
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    directory = (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/"
    )
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": ["13F-HR"],
                "filingDate": ["2026-05-15"],
                "primaryDocument": ["primary.htm"],
                "reportDate": ["2026-03-31"],
            }
        }
    }
    index = {"directory": {"item": [{"name": "infotable.xml"}]}}
    xml = """<informationTable><infoTable>
      <nameOfIssuer>Example Corp</nameOfIssuer><titleOfClass>COM</titleOfClass>
      <cusip>123456789</cusip><value>2500</value>
      <sshPrnamt>10000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>
      <investmentDiscretion>SOLE</investmentDiscretion>
    </infoTable></informationTable>"""
    session = FakeSession(
        {
            submissions_url: submissions,
            directory + "index.json": index,
            directory + "infotable.xml": xml,
        }
    )
    adapter = SecEdgarFilingsAdapter(
        ciks=[cik],
        user_agent="AITOS research contact@example.org",
        revision_rescan_filings_per_poll=1,
        session=session,
    )

    first_events, cursor = await adapter.poll()
    second_events, next_cursor = await adapter.poll(cursor=cursor)
    assert len(first_events) == len(second_events) == 1
    assert first_events[0].event_id == second_events[0].event_id
    assert json.loads(next_cursor)[cik] == accession
    assert session.requests.count(directory + "index.json") == 2
