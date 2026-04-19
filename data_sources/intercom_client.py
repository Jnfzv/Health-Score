from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.intercom.io"
_PAGE_SIZE = 50
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0  # seconds; doubles on each attempt

# Ordered from most specific to least to reduce false positives.
_CHURN_KEYWORDS: tuple[str, ...] = (
    "darme de baja",
    "dejar harbiz",
    "cancelación",
    "no renuevo",
    "no renovar",
    "cancelar",
    "cancela",
    "me voy",
    "baja",
)


class IntercomDataExtractor:
    def __init__(self, token: str | None = None) -> None:
        _token = token or os.environ["INTERCOM_API_TOKEN"]
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {_token}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        })
        logger.info("IntercomDataExtractor ready.")

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Execute an HTTP request with exponential-backoff retry."""
        url = f"{_BASE_URL}{path}"
        delay = _BASE_BACKOFF
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)

                if resp.status_code == 429:
                    # Honour the server's Retry-After when present.
                    retry_after = float(resp.headers.get("Retry-After", delay))
                    logger.warning(
                        "Rate limit hit (attempt %d/%d). Backing off %.1fs.",
                        attempt, _MAX_RETRIES, retry_after,
                    )
                    if attempt < _MAX_RETRIES:
                        time.sleep(retry_after)
                        delay = max(delay * 2, retry_after)
                        continue

                resp.raise_for_status()
                return resp.json()

            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status in {500, 502, 503, 504} and attempt < _MAX_RETRIES:
                    logger.warning(
                        "HTTP %s (attempt %d/%d). Backing off %.1fs.",
                        status, attempt, _MAX_RETRIES, delay,
                    )
                    last_exc = exc
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise

            except requests.ConnectionError as exc:
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "Connection error (attempt %d/%d). Backing off %.1fs.",
                        attempt, _MAX_RETRIES, delay,
                    )
                    last_exc = exc
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise

        raise RuntimeError(
            f"Intercom request failed after {_MAX_RETRIES} attempts."
        ) from last_exc

    def _search_pages(self, query: dict) -> Iterator[dict]:
        """
        Yield every conversation dict from a paginated /conversations/search call.
        Intercom cursor pagination: response.pages.next.starting_after
        """
        starting_after: str | None = None
        page_num = 0

        while True:
            payload: dict[str, Any] = {
                "query":      query,
                "pagination": {"per_page": _PAGE_SIZE},
            }
            if starting_after:
                payload["pagination"]["starting_after"] = starting_after

            data = self._request("POST", "/conversations/search", json=payload)
            page_num += 1

            batch: list[dict] = data.get("conversations", [])
            yield from batch

            logger.debug(
                "Page %d: %d conversations. Has more: %s",
                page_num,
                len(batch),
                bool(data.get("pages", {}).get("next")),
            )

            next_cursor = (data.get("pages") or {}).get("next")
            if not isinstance(next_cursor, dict):
                break
            starting_after = next_cursor.get("starting_after")
            if not starting_after or not batch:
                break

    # ------------------------------------------------------------------
    # Field-extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _email(conv: dict) -> str | None:
        """
        Extract contact email from a conversation object.

        Priority:
          1. source.author.email  — available for most inbound conversations
          2. contacts.contacts[0].email  — available when contact is expanded
        """
        author = ((conv.get("source") or {}).get("author") or {})
        if author.get("email"):
            return author["email"]

        contacts = ((conv.get("contacts") or {}).get("contacts") or [])
        if contacts:
            return contacts[0].get("email")

        return None

    @staticmethod
    def _tags(conv: dict) -> list[str]:
        return [
            t["name"]
            for t in ((conv.get("tags") or {}).get("tags") or [])
            if t.get("name")
        ]

    @staticmethod
    def _ts(unix: int | None) -> datetime | None:
        if unix is None:
            return None
        return datetime.fromtimestamp(unix, tz=timezone.utc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_recent_conversations(self, days: int = 30) -> pd.DataFrame:
        """
        Return all conversations created in the last `days` days.

        Columns: conversation_id, contact_email, created_at, state,
                 tags, first_contact_reply
        """
        logger.info("Fetching conversations from the last %d days…", days)
        since = int(
            (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp()
        )

        query = {"field": "created_at", "operator": ">", "value": since}

        rows: list[dict] = []
        for conv in self._search_pages(query):
            rows.append({
                "conversation_id":     conv.get("id"),
                "contact_email":       self._email(conv),
                "created_at":          self._ts(conv.get("created_at")),
                "state":               conv.get("state"),
                "tags":                self._tags(conv),
                "first_contact_reply": conv.get("first_contact_reply"),
            })

        df = pd.DataFrame(rows)
        logger.info("Fetched %d conversations.", len(df))
        return df

    def count_tickets_by_customer(self, days: int = 30) -> pd.DataFrame:
        """
        Aggregate ticket activity per customer email over the last `days` days.

        open_tickets_48h counts open conversations whose created_at is older
        than 48 h — i.e. unresolved tickets that have exceeded the SLA window.

        Columns: email, ticket_count, open_tickets_48h
        """
        logger.info("Counting tickets by customer for the last %d days…", days)
        df = self.get_recent_conversations(days=days)

        if df.empty:
            return pd.DataFrame(columns=["email", "ticket_count", "open_tickets_48h"])

        cutoff_48h = datetime.now(tz=timezone.utc) - timedelta(hours=48)
        df["stale_open"] = (
            (df["state"] == "open") & (df["created_at"] < cutoff_48h)
        )

        agg = (
            df.groupby("contact_email", dropna=True)
            .agg(
                ticket_count=("conversation_id", "count"),
                open_tickets_48h=("stale_open", "sum"),
            )
            .reset_index()
            .rename(columns={"contact_email": "email"})
        )
        agg["open_tickets_48h"] = agg["open_tickets_48h"].astype(int)

        logger.info("Aggregated tickets for %d customers.", len(agg))
        return agg

    def detect_churn_mentions(self, days: int = 14) -> pd.DataFrame:
        """
        Identify customers who mentioned churn-intent keywords in the last
        `days` days. A single search request with an OR clause covers all
        keywords, minimising API calls.

        Columns: email, churn_mention (always True)
        """
        logger.info(
            "Scanning for churn-intent keywords in the last %d days…", days
        )
        since = int(
            (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp()
        )

        query = {
            "operator": "AND",
            "value": [
                {"field": "created_at", "operator": ">",        "value": since},
                {
                    "operator": "OR",
                    "value": [
                        {"field": "body", "operator": "CONTAINS", "value": kw}
                        for kw in _CHURN_KEYWORDS
                    ],
                },
            ],
        }

        churn_emails: set[str] = set()
        for conv in self._search_pages(query):
            email = self._email(conv)
            if email:
                churn_emails.add(email)
                logger.debug(
                    "Churn mention: conversation=%s email=%s",
                    conv.get("id"), email,
                )

        df = pd.DataFrame(
            [{"email": e, "churn_mention": True} for e in churn_emails]
        )
        logger.info(
            "Churn scan complete: %d customer(s) flagged.", len(df)
        )
        return df
