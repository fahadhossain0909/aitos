"""Official SEC EDGAR 13F-HR and Forms 3/4/5 disclosure adapter.

This adapter ingests public filings only. Filing/report dates remain distinct and
reported holdings are never treated as real-time positions or trading instructions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from defusedxml import ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .contracts import CapitalEvent


class SecEdgarFilingsAdapter:
    DATA_URL = "https://data.sec.gov"
    ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/"

    def __init__(
        self,
        *,
        ciks: list[str],
        user_agent: str,
        forms: tuple[str, ...] = (
            "13F-HR",
            "13F-HR/A",
            "3",
            "3/A",
            "4",
            "4/A",
            "5",
            "5/A",
        ),
        timeout_seconds: float = 12.0,
        max_filings_per_poll: int = 25,
        initial_filings_per_cik: int = 10,
        revision_rescan_filings_per_poll: int = 5,
        session: aiohttp.ClientSession | None = None,
    ):
        normalized = sorted({str(int(cik)).zfill(10) for cik in ciks})
        if not normalized or len(normalized) > 500:
            raise ValueError("provide 1-500 SEC CIKs")
        if not user_agent.strip() or "@" not in user_agent:
            raise ValueError(
                "SEC requires an identifying User-Agent with contact email"
            )
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        if (
            not 1 <= max_filings_per_poll <= 100
            or not 1 <= initial_filings_per_cik <= 100
            or isinstance(revision_rescan_filings_per_poll, bool)
            or not 0 <= revision_rescan_filings_per_poll <= 25
        ):
            raise ValueError("invalid filing bounds")
        self.ciks = normalized
        self.user_agent = user_agent.strip()
        self.forms = set(forms)
        self.timeout_seconds = timeout_seconds
        self.max_filings_per_poll = max_filings_per_poll
        self.initial_filings_per_cik = initial_filings_per_cik
        self.revision_rescan_filings_per_poll = revision_rescan_filings_per_poll
        self.session = session
        self.owns_session = session is None
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def close(self) -> None:
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _get_text(self, url: str) -> str:
        async with self._request_lock:
            wait = 0.11 - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            if self.session is None:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept-Encoding": "gzip, deflate",
                        "Accept": "application/json, application/xml, text/xml, */*",
                    },
                )
            async with self.session.get(url) as response:
                if response.status == 429:
                    raise RuntimeError("SEC EDGAR rate limit reached")
                response.raise_for_status()
                body = await response.text()
            self._last_request_at = time.monotonic()
        if len(body) > 10_000_000:
            raise RuntimeError("SEC filing payload exceeds 10 MB safety limit")
        if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
            raise RuntimeError("SEC XML contains prohibited DTD/entity declarations")
        return body

    async def _get_json(self, url: str) -> dict[str, Any]:
        payload = json.loads(await self._get_text(url))
        if not isinstance(payload, dict):
            raise RuntimeError("SEC endpoint returned an unexpected JSON document")
        return payload

    @staticmethod
    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @classmethod
    def _value(cls, node: ET.Element, name: str) -> str | None:
        for child in node.iter():
            if cls._local(child.tag) == name:
                for nested in child:
                    if cls._local(nested.tag) == "value" and nested.text:
                        return nested.text.strip()
                if child.text and child.text.strip():
                    return child.text.strip()
        return None

    @staticmethod
    def _day(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.combine(
                date.fromisoformat(value[:10]), datetime.min.time(), timezone.utc
            )
        except ValueError:
            return None

    @staticmethod
    def _event_id(*parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    async def _information_table_url(
        self, cik: str, accession: str, primary: str
    ) -> str:
        directory = f"{self.ARCHIVES_URL}{int(cik)}/{accession.replace('-', '')}/"
        index = await self._get_json(urljoin(directory, "index.json"))
        items = (index.get("directory") or {}).get("item") or []
        candidates = [
            item.get("name", "")
            for item in items
            if str(item.get("name", "")).lower().endswith(".xml")
            and "infotable" in str(item.get("name", "")).lower()
        ]
        document = candidates[0] if candidates else primary
        if not document.lower().endswith(".xml"):
            raise RuntimeError("13F filing information table XML was not found")
        return urljoin(directory, document)

    async def _parse_13f(
        self,
        cik: str,
        accession: str,
        form: str,
        filing_date: datetime,
        report_date: str,
        url: str,
    ) -> list[CapitalEvent]:
        root = ET.fromstring(await self._get_text(url))
        period = self._day(self._value(root, "periodOfReport") or report_date)
        if period is None:
            raise RuntimeError("13F filing has no valid period-of-report date")
        events = []
        for index, item in enumerate(
            node for node in root.iter() if self._local(node.tag) == "infoTable"
        ):
            cusip = self._value(item, "cusip")
            shares = self._value(item, "sshPrnamt")
            value_thousands = self._value(item, "value")
            if not cusip or not shares:
                continue
            quantity = float(shares)
            reported_value = (
                float(value_thousands) * 1000.0 if value_thousands is not None else None
            )
            if quantity < 0 or (reported_value is not None and reported_value < 0):
                raise RuntimeError("13F filing contains a negative holding")
            event_id = self._event_id(
                "13f", cik, accession, cusip, str(index)
            )
            events.append(
                CapitalEvent(
                    event_id,
                    "sec-edgar",
                    "sec_edgar_13f",
                    "equities",
                    "sec_edgar",
                    cusip,
                    "13f_holding_report",
                    period,
                    filing_date,
                    quantity,
                    "shares",
                    reported_value,
                    actor_id=f"sec-cik:{int(cik)}",
                    counterparty_id=f"cusip:{cusip}",
                    transaction_id=accession,
                    finality="confirmed",
                    metadata={
                        "filing_form": form,
                        "accession_number": accession,
                        "filing_url": url,
                        "filing_date": filing_date.isoformat(),
                        "period_of_report": period.isoformat(),
                        "reporting_lag_days": (filing_date - period).days,
                        "actor_identity_kind": "sec_institution",
                        "issuer_name": self._value(item, "nameOfIssuer"),
                        "title_of_class": self._value(item, "titleOfClass"),
                        "reported_value_unit": "USD_thousands_as_filed",
                        "reported_value_usd": reported_value,
                        "put_call": self._value(item, "putCall"),
                        "investment_discretion": self._value(
                            item, "investmentDiscretion"
                        ),
                        "reported_shares_type": self._value(item, "sshPrnamtType"),
                        "source_semantics": "quarterly_disclosed_holding_not_realtime_position",
                    },
                )
            )
        return events

    async def _parse_form4(
        self, cik: str, accession: str, filing_date: datetime, form: str, url: str
    ) -> list[CapitalEvent]:
        root = ET.fromstring(await self._get_text(url))
        owner_cik = self._value(root, "rptOwnerCik")
        issuer_symbol = self._value(root, "issuerTradingSymbol")
        issuer_id = self._value(root, "issuerCik") or cik
        events = []
        if form in {"3", "3/A"}:
            period = self._day(self._value(root, "periodOfReport")) or filing_date
            holdings = [
                node
                for node in root.iter()
                if self._local(node.tag) == "nonDerivativeHolding"
            ]
            for index, item in enumerate(holdings):
                shares_raw = self._value(item, "sharesOwnedFollowingTransaction")
                if shares_raw is None:
                    continue
                shares = float(shares_raw)
                if shares < 0:
                    raise RuntimeError("Form 3 contains a negative holding")
                security_title = self._value(item, "securityTitle")
                event_id = self._event_id(
                    "form3",
                    cik,
                    accession,
                    str(index),
                )
                events.append(
                    CapitalEvent(
                        event_id,
                        "sec-edgar",
                        "sec_edgar_form3",
                        "equities",
                        "sec_edgar",
                        issuer_symbol or f"CIK{int(issuer_id):010d}",
                        "insider_holding_disclosure",
                        period,
                        filing_date,
                        shares,
                        "shares",
                        None,
                        actor_id=f"sec-cik:{int(owner_cik)}" if owner_cik else None,
                        counterparty_id=f"sec-cik:{int(issuer_id)}",
                        transaction_id=accession,
                        finality="confirmed",
                        metadata={
                            "filing_form": form,
                            "accession_number": accession,
                            "filing_url": url,
                            "filing_date": filing_date.isoformat(),
                            "period_of_report": period.isoformat(),
                            "security_title": security_title,
                            "direct_or_indirect": self._value(
                                item, "directOrIndirectOwnership"
                            ),
                            "flow_sign": 0,
                            "actor_identity_kind": "sec_insider",
                            "source_semantics": "initial_insider_holding_snapshot_not_transaction_flow",
                        },
                    )
                )
            return events

        transaction_nodes = [
            node
            for node in root.iter()
            if self._local(node.tag) == "nonDerivativeTransaction"
        ]
        for index, item in enumerate(transaction_nodes):
            transaction_date = self._day(self._value(item, "transactionDate"))
            shares_raw = self._value(item, "transactionShares")
            price_raw = self._value(item, "transactionPricePerShare")
            if transaction_date is None or shares_raw is None:
                continue
            shares = float(shares_raw)
            price = float(price_raw) if price_raw not in (None, "") else None
            if shares < 0 or (price is not None and price < 0):
                raise RuntimeError("Form 4 contains a negative transaction value")
            acquired_disposed = self._value(item, "transactionAcquiredDisposedCode")
            code = self._value(item, "transactionCode")
            sign = (
                1
                if acquired_disposed == "A"
                else (-1 if acquired_disposed == "D" else 0)
            )
            event_id = self._event_id(
                "form4",
                cik,
                accession,
                str(index),
            )
            events.append(
                CapitalEvent(
                    event_id,
                    "sec-edgar",
                    "sec_edgar_form4",
                    "equities",
                    "sec_edgar",
                    issuer_symbol or f"CIK{int(issuer_id):010d}",
                    "insider_transaction",
                    transaction_date,
                    filing_date,
                    shares,
                    "shares",
                    shares * price if price is not None else None,
                    actor_id=f"sec-cik:{int(owner_cik)}" if owner_cik else None,
                    counterparty_id=f"sec-cik:{int(issuer_id)}",
                    transaction_id=accession,
                    finality="confirmed",
                    metadata={
                        "filing_form": form,
                        "accession_number": accession,
                        "filing_url": url,
                        "filing_date": filing_date.isoformat(),
                        "transaction_date": transaction_date.isoformat(),
                        "transaction_code": code,
                        "acquired_disposed_code": acquired_disposed,
                        "flow_sign": sign,
                        "price_per_share": price,
                        "reporting_owner_cik": owner_cik,
                        "reporting_lag_days": (filing_date - transaction_date).days,
                        "actor_identity_kind": "sec_insider",
                        "issuer_cik": issuer_id,
                        "source_semantics": "reported_insider_transaction_not_a_standalone_signal",
                    },
                )
            )
        return events

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[CapitalEvent], str | None]:
        if cursor is None:
            previous: dict[str, str] = {}
        else:
            try:
                previous = json.loads(cursor)
            except (TypeError, ValueError):
                raise ValueError(
                    "SEC cursor must be a JSON CIK-to-accession map"
                ) from None
            if not isinstance(previous, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in previous.items()
            ):
                raise ValueError("invalid SEC cursor map")

        events: list[CapitalEvent] = []
        next_cursor = dict(previous)
        processed_filings = 0
        for cik in self.ciks:
            submissions = await self._get_json(
                f"{self.DATA_URL}/submissions/CIK{cik}.json"
            )
            recent = (submissions.get("filings") or {}).get("recent") or {}
            accessions = recent.get("accessionNumber") or []
            forms = recent.get("form") or []
            dates = recent.get("filingDate") or []
            primary_docs = recent.get("primaryDocument") or []
            report_dates = recent.get("reportDate") or []
            rows = []
            for index, accession in enumerate(accessions):
                if index >= min(
                    len(forms), len(dates), len(primary_docs), len(report_dates)
                ):
                    break
                rows.append(
                    {
                        "accession": accession,
                        "form": forms[index],
                        "filing_date": dates[index],
                        "primary": primary_docs[index],
                        "report_date": report_dates[index],
                    }
                )
            if not rows:
                continue
            latest = rows[0]["accession"]
            old = previous.get(cik)
            if old:
                positions = [i for i, row in enumerate(rows) if row["accession"] == old]
                if not positions:
                    if latest != old:
                        raise RuntimeError(
                            f"SEC cursor for CIK {cik} is outside the recent submissions window; controlled backfill required"
                        )
                    selected = []
                else:
                    selected = rows[: positions[0]]
            else:
                selected = rows[: self.initial_filings_per_cik]
            work = [(row, True) for row in reversed(selected)]
            seen_accessions = {row["accession"] for row, _ in work}
            # Re-read a small recent window every poll so corrected/updated
            # filing documents are observed even when their accession is known.
            for row in reversed(rows[: self.revision_rescan_filings_per_poll]):
                if row["accession"] not in seen_accessions:
                    work.append((row, False))
                    seen_accessions.add(row["accession"])

            stop_batch = False
            for row, advances_cursor in work:
                if row["form"] not in self.forms:
                    if advances_cursor:
                        next_cursor[cik] = row["accession"]
                    continue
                if processed_filings >= self.max_filings_per_poll:
                    stop_batch = True
                    break
                processed_filings += 1
                filing_date = self._day(row["filing_date"])
                if filing_date is None:
                    raise RuntimeError("SEC filing date is invalid")
                accession = row["accession"]
                directory = (
                    f"{self.ARCHIVES_URL}{int(cik)}/{accession.replace('-', '')}/"
                )
                if row["form"].startswith("13F-HR"):
                    document_url = await self._information_table_url(
                        cik, accession, row["primary"]
                    )
                    events.extend(
                        await self._parse_13f(
                            cik,
                            accession,
                            row["form"],
                            filing_date,
                            row["report_date"],
                            document_url,
                        )
                    )
                elif row["form"] in {"3", "3/A", "4", "4/A", "5", "5/A"}:
                    document_url = urljoin(directory, row["primary"])
                    events.extend(
                        await self._parse_form4(
                            cik, accession, filing_date, row["form"], document_url
                        )
                    )
                if advances_cursor:
                    next_cursor[cik] = accession
            if stop_batch:
                break
        serialized = json.dumps(next_cursor, sort_keys=True, separators=(",", ":"))
        if len(serialized) > 262144:
            raise ValueError("SEC cursor exceeds safe size")
        return events, serialized
