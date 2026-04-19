from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd
import stripe
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0  # seconds; doubles on each attempt


class StripeDataExtractor:
    def __init__(self, api_key: str | None = None) -> None:
        stripe.api_key = api_key or os.environ["STRIPE_API_KEY"]
        logger.info("StripeDataExtractor ready.")

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _call(self, fn: Callable, **kwargs):
        """Invoke a Stripe SDK callable with exponential-backoff retry."""
        delay = _BASE_BACKOFF
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return fn(**kwargs)

            except stripe.RateLimitError as exc:
                last_exc = exc
                logger.warning(
                    "Rate limit (attempt %d/%d). Backing off %.1fs.",
                    attempt, _MAX_RETRIES, delay,
                )

            except stripe.APIConnectionError as exc:
                last_exc = exc
                logger.warning(
                    "Connection error (attempt %d/%d): %s. Backing off %.1fs.",
                    attempt, _MAX_RETRIES, exc, delay,
                )

            except stripe.APIError as exc:
                # Only retry on transient server-side errors.
                if exc.http_status not in {429, 500, 502, 503, 504}:
                    raise
                last_exc = exc
                logger.warning(
                    "Stripe API error HTTP %s (attempt %d/%d). Backing off %.1fs.",
                    exc.http_status, attempt, _MAX_RETRIES, delay,
                )

            if attempt < _MAX_RETRIES:
                time.sleep(delay)
                delay *= 2

        raise RuntimeError(
            f"Stripe call failed after {_MAX_RETRIES} attempts."
        ) from last_exc

    def _paginate(self, list_fn: Callable, **kwargs):
        """Yield every object from a paginated Stripe list endpoint."""
        params: dict = {"limit": _PAGE_SIZE, **kwargs}
        page_num = 0

        while True:
            page = self._call(list_fn, **params)
            page_num += 1
            yield from page.data

            if not page.has_more:
                break

            params["starting_after"] = page.data[-1].id
            logger.debug(
                "Page %d fetched (%d items); continuing after %s.",
                page_num, len(page.data), page.data[-1].id,
            )

    # ------------------------------------------------------------------
    # MRR normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _to_mrr(amount_cents: int, interval: str, interval_count: int) -> float:
        """Normalise any billing cadence to a monthly amount."""
        amount = amount_cents / 100
        match interval:
            case "month": return amount / interval_count
            case "year":  return amount / (12 * interval_count)
            case "week":  return amount * (52 / 12) / interval_count
            case "day":   return amount * (365 / 12) / interval_count
            case _:       return amount

    # ------------------------------------------------------------------
    # Customer / subscription field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _customer_id(raw) -> str:
        return raw.id if isinstance(raw, stripe.Customer) else raw

    @staticmethod
    def _customer_email(raw) -> str | None:
        return raw.email if isinstance(raw, stripe.Customer) else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_all_active_subscriptions(self) -> pd.DataFrame:
        """
        Fetch every subscription regardless of status.

        Returns a DataFrame with one row per subscription containing
        billing, plan, and lifecycle metadata.
        """
        logger.info("Fetching all subscriptions (status=all)…")
        rows: list[dict] = []

        for sub in self._paginate(
            stripe.Subscription.list,
            status="all",
            expand=["data.customer", "data.items.data.price.product"],
        ):
            item = sub.items.data[0] if sub.items.data else None
            price = getattr(item, "price", None)
            product = getattr(price, "product", None)
            recurring = getattr(price, "recurring", None)

            plan_name: str | None = (
                product.name if isinstance(product, stripe.Product) else None
            )

            mrr: float | None = None
            if price and price.unit_amount is not None:
                mrr = self._to_mrr(
                    price.unit_amount,
                    getattr(recurring, "interval", "month"),
                    getattr(recurring, "interval_count", 1),
                )

            rows.append({
                "subscription_id":      sub.id,
                "customer_id":          self._customer_id(sub.customer),
                "customer_email":       self._customer_email(sub.customer),
                "status":               sub.status,
                "plan_name":            plan_name,
                "mrr":                  mrr,
                "created":              datetime.fromtimestamp(sub.created, tz=timezone.utc),
                "current_period_start": datetime.fromtimestamp(sub.current_period_start, tz=timezone.utc),
                "current_period_end":   datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc),
                "pause_collection":     sub.pause_collection,
                "cancel_at_period_end": sub.cancel_at_period_end,
            })

        df = pd.DataFrame(rows)
        logger.info("Retrieved %d subscriptions.", len(df))
        return df

    def get_payment_failures(
        self,
        customer_ids: list[str],
        days: int = 90,
    ) -> pd.DataFrame:
        """
        Count failed charge attempts per customer over the last `days` days.

        An attempt is counted when an invoice has status 'open' or 'uncollectible'
        and has been retried at least once (attempt_count > 0).

        Returns a DataFrame with columns [customer_id, failure_count].
        """
        logger.info(
            "Scanning payment failures for %d customers over the last %d days…",
            len(customer_ids), days,
        )
        since = int(
            (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp()
        )
        rows: list[dict] = []

        for idx, customer_id in enumerate(customer_ids, start=1):
            failure_count = 0

            for status in ("open", "uncollectible"):
                for invoice in self._paginate(
                    stripe.Invoice.list,
                    customer=customer_id,
                    status=status,
                    created={"gte": since},
                ):
                    failure_count += invoice.attempt_count or 0

            if idx % 50 == 0:
                logger.debug(
                    "Payment failure scan progress: %d/%d customers processed.",
                    idx, len(customer_ids),
                )

            rows.append({"customer_id": customer_id, "failure_count": failure_count})

        df = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["customer_id", "failure_count"]
        )
        customers_with_failures = (df["failure_count"] > 0).sum()
        logger.info(
            "Payment failure scan complete: %d/%d customers have failures.",
            customers_with_failures, len(df),
        )
        return df

    def detect_ghost_invoices(self) -> pd.DataFrame:
        """
        Identify accounts with an active pause_collection that also have
        at least one draft invoice — i.e. billing is accumulating silently
        while the account is paused.

        Returns a DataFrame with columns [customer_id, ghost_flag].
        """
        logger.info("Scanning for ghost invoices (paused subs with draft invoices)…")
        ghost_ids: set[str] = set()

        for sub in self._paginate(stripe.Subscription.list, status="all"):
            if not sub.pause_collection:
                continue

            customer_id = self._customer_id(sub.customer)

            probe = self._call(
                stripe.Invoice.list,
                subscription=sub.id,
                status="draft",
                limit=1,
            )
            if probe.data:
                ghost_ids.add(customer_id)
                logger.debug(
                    "Ghost account: customer=%s subscription=%s",
                    customer_id, sub.id,
                )

        df = pd.DataFrame(
            [{"customer_id": cid, "ghost_flag": True} for cid in ghost_ids]
        )
        logger.info("Ghost invoice scan complete: %d accounts flagged.", len(df))
        return df
