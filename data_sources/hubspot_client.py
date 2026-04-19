from __future__ import annotations

import logging
import os
import time
from typing import Iterator

import hubspot
import pandas as pd
from dotenv import load_dotenv
from hubspot.crm.associations import BatchInputPublicObjectId, PublicObjectId
from hubspot.crm.contacts import (
    BatchInputSimplePublicObjectBatchInput,
    BatchReadInputSimplePublicObjectId,
    SimplePublicObjectBatchInput,
    SimplePublicObjectId,
)
from hubspot.crm.deals import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)
from hubspot.crm.properties import PropertyCreate

load_dotenv()

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0
_SEARCH_PAGE_SIZE = 100
_BATCH_SIZE = 100  # HubSpot max for batch operations

_PIPELINE_CANCEL: str = "772547709"
_PIPELINE_UNPAID: str = "74464769"
_HEALTH_SCORE_PROP: str = "health_score"
_HEALTH_SCORE_GROUP: str = "contactinformation"


def _chunks(lst: list, n: int) -> Iterator[list]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


class HubSpotDataExtractor:
    def __init__(self, access_token: str | None = None) -> None:
        token = access_token or os.environ["HUBSPOT_API_KEY"]
        self._client = hubspot.HubSpot(access_token=token)
        logger.info("HubSpotDataExtractor ready.")

    # ------------------------------------------------------------------
    # Low-level: retry wrapper
    # ------------------------------------------------------------------

    def _call(self, fn, *args, **kwargs):
        """
        Call any HubSpot SDK method with exponential-backoff retry on 429.
        Respects the Retry-After response header when present.
        """
        delay = _BASE_BACKOFF
        last_exc: ApiException | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except ApiException as exc:
                if exc.status != 429 or attempt == _MAX_RETRIES:
                    raise
                retry_after = float((exc.headers or {}).get("Retry-After", delay))
                logger.warning(
                    "Rate limit hit (attempt %d/%d). Backing off %.1fs.",
                    attempt, _MAX_RETRIES, retry_after,
                )
                time.sleep(retry_after)
                delay = max(delay * 2, retry_after)
                last_exc = exc

        raise RuntimeError(
            f"HubSpot call failed after {_MAX_RETRIES} attempts."
        ) from last_exc

    # ------------------------------------------------------------------
    # Internal: deals search
    # ------------------------------------------------------------------

    def _search_all_deals(self, pipeline_id: str) -> list:
        """Paginate through all deals in a pipeline. Returns list of result objects."""
        deals = []
        after: str | None = None

        while True:
            req = PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(filters=[
                        Filter(
                            property_name="pipeline",
                            operator="EQ",
                            value=pipeline_id,
                        )
                    ])
                ],
                properties=["dealname", "dealstage", "createdate"],
                limit=_SEARCH_PAGE_SIZE,
                **( {"after": after} if after else {} ),
            )
            resp = self._call(
                self._client.crm.deals.search_api.do_search,
                public_object_search_request=req,
            )
            deals.extend(resp.results)
            logger.debug(
                "Pipeline %s: %d deals fetched so far.", pipeline_id, len(deals)
            )

            if resp.paging and resp.paging.next:
                after = resp.paging.next.after
            else:
                break

        return deals

    # ------------------------------------------------------------------
    # Internal: deal → contact email resolution
    # ------------------------------------------------------------------

    def _contact_emails_for_deals(
        self, deal_ids: list[str]
    ) -> dict[str, str | None]:
        """
        Return {deal_id: contact_email} using two batched API calls:
          1. associations batch read  → deal_id → contact_id(s)
          2. contacts batch read      → contact_id → email

        Falls back to None for deals with no associated contact.
        """
        if not deal_ids:
            return {}

        # --- Step 1: batch-fetch associations (deals → contacts) ----------
        deal_to_contact_ids: dict[str, list[str]] = {d: [] for d in deal_ids}

        for chunk in _chunks(deal_ids, _BATCH_SIZE):
            resp = self._call(
                self._client.crm.associations.batch_api.read,
                from_object_type="deals",
                to_object_type="contacts",
                batch_input_public_object_id=BatchInputPublicObjectId(
                    inputs=[PublicObjectId(id=d) for d in chunk]
                ),
            )
            for item in resp.results:
                from_id: str = item._from.id
                # SDK v12: `to` is a list; guard against single-object variant.
                to_items = item.to if isinstance(item.to, list) else [item.to]
                deal_to_contact_ids[from_id] = [a.id for a in to_items if a]

        # --- Step 2: batch-read contacts to get emails --------------------
        all_contact_ids = list(
            {cid for cids in deal_to_contact_ids.values() for cid in cids}
        )
        contact_email: dict[str, str] = {}

        for chunk in _chunks(all_contact_ids, _BATCH_SIZE):
            resp = self._call(
                self._client.crm.contacts.batch_api.read,
                batch_read_input_simple_public_object_id=BatchReadInputSimplePublicObjectId(
                    inputs=[SimplePublicObjectId(id=cid) for cid in chunk],
                    properties=["email"],
                ),
            )
            for obj in resp.results:
                email = (obj.properties or {}).get("email")
                if email:
                    contact_email[obj.id] = email

        # --- Step 3: join ------------------------------------------------
        return {
            deal_id: contact_email.get(cids[0]) if cids else None
            for deal_id, cids in deal_to_contact_ids.items()
        }

    def _deals_df(self, pipeline_id: str) -> pd.DataFrame:
        """Shared implementation for both pipeline methods."""
        deals = self._search_all_deals(pipeline_id)
        if not deals:
            return pd.DataFrame(
                columns=["deal_id", "contact_email", "deal_stage", "create_date"]
            )

        email_map = self._contact_emails_for_deals([d.id for d in deals])

        rows = [
            {
                "deal_id":       deal.id,
                "contact_email": email_map.get(deal.id),
                "deal_stage":    (deal.properties or {}).get("dealstage"),
                "create_date":   (deal.properties or {}).get("createdate"),
            }
            for deal in deals
        ]

        df = pd.DataFrame(rows)
        df["create_date"] = pd.to_datetime(df["create_date"], errors="coerce", utc=True)
        return df

    # ------------------------------------------------------------------
    # Internal: custom property management
    # ------------------------------------------------------------------

    def _ensure_health_score_property(self) -> None:
        """Create the health_score contact property if it does not already exist."""
        try:
            self._call(
                self._client.crm.properties.core_api.get_by_name,
                object_type="contacts",
                property_name=_HEALTH_SCORE_PROP,
            )
            logger.debug("Property '%s' already exists.", _HEALTH_SCORE_PROP)
            return
        except ApiException as exc:
            if exc.status != 404:
                raise

        logger.info("Creating HubSpot property '%s'…", _HEALTH_SCORE_PROP)
        self._call(
            self._client.crm.properties.core_api.create,
            object_type="contacts",
            property_create=PropertyCreate(
                name=_HEALTH_SCORE_PROP,
                label="Health Score",
                type="number",
                field_type="number",
                group_name=_HEALTH_SCORE_GROUP,
                description="Customer Health Score (0–100). Computed automatically.",
            ),
        )
        logger.info("Property '%s' created.", _HEALTH_SCORE_PROP)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_deals_in_cancel_pipeline(self) -> pd.DataFrame:
        """
        Return all deals in the 'plan_to_cancel' pipeline.

        Columns: deal_id, contact_email, deal_stage, create_date
        """
        logger.info("Fetching deals from cancel pipeline (%s)…", _PIPELINE_CANCEL)
        df = self._deals_df(_PIPELINE_CANCEL)
        logger.info("Cancel pipeline: %d deals found.", len(df))
        return df

    def get_deals_in_unpaid_pipeline(self) -> pd.DataFrame:
        """
        Return all deals in the 'unpaid' pipeline.

        Columns: deal_id, contact_email, deal_stage, create_date
        """
        logger.info("Fetching deals from unpaid pipeline (%s)…", _PIPELINE_UNPAID)
        df = self._deals_df(_PIPELINE_UNPAID)
        logger.info("Unpaid pipeline: %d deals found.", len(df))
        return df

    def sync_health_score(self, scores_df: pd.DataFrame) -> None:
        """
        Write health_score values back to HubSpot contacts.

        Expects a DataFrame with at minimum columns: email, health_score.
        - Creates the custom property if it doesn't already exist.
        - Skips rows with missing email or score.
        - Uses batched reads (by email) + batched updates to minimise API calls.
        - Logs a final summary of updated vs. not-found contacts.
        """
        required = {"email", "health_score"}
        if not required.issubset(scores_df.columns):
            raise ValueError(
                f"scores_df must contain columns {required}. "
                f"Got: {list(scores_df.columns)}"
            )

        valid = scores_df.dropna(subset=["email", "health_score"]).copy()
        if valid.empty:
            logger.warning("sync_health_score: no valid rows to sync.")
            return

        logger.info(
            "Syncing health scores for %d contacts to HubSpot…", len(valid)
        )
        self._ensure_health_score_property()

        updated = 0
        not_found = 0

        for chunk in _chunks(valid.to_dict("records"), _BATCH_SIZE):
            emails = [row["email"] for row in chunk]
            score_map: dict[str, float] = {row["email"]: row["health_score"] for row in chunk}

            # Batch-read contacts using email as the lookup key.
            try:
                read_resp = self._call(
                    self._client.crm.contacts.batch_api.read,
                    batch_read_input_simple_public_object_id=BatchReadInputSimplePublicObjectId(
                        id_property="email",
                        inputs=[SimplePublicObjectId(id=e) for e in emails],
                        properties=["email"],
                    ),
                )
            except ApiException as exc:
                logger.error(
                    "Batch contact read failed (HTTP %s); skipping chunk of %d.",
                    exc.status, len(emails),
                )
                continue

            email_to_id = {
                (obj.properties or {}).get("email"): obj.id
                for obj in read_resp.results
                if (obj.properties or {}).get("email")
            }
            chunk_not_found = len(emails) - len(email_to_id)
            not_found += chunk_not_found
            if chunk_not_found:
                missing = set(emails) - set(email_to_id)
                logger.warning(
                    "%d email(s) not found in HubSpot: %s",
                    chunk_not_found,
                    ", ".join(sorted(missing)),
                )

            updates = [
                SimplePublicObjectBatchInput(
                    id=contact_id,
                    properties={
                        _HEALTH_SCORE_PROP: str(round(score_map[email], 2))
                    },
                )
                for email, contact_id in email_to_id.items()
                if email in score_map
            ]
            if not updates:
                continue

            try:
                self._call(
                    self._client.crm.contacts.batch_api.update,
                    batch_input_simple_public_object_batch_input=BatchInputSimplePublicObjectBatchInput(
                        inputs=updates
                    ),
                )
                updated += len(updates)
            except ApiException as exc:
                logger.error(
                    "Batch contact update failed (HTTP %s); skipping %d contacts.",
                    exc.status, len(updates),
                )

        logger.info(
            "sync_health_score done — updated: %d, not found: %d.",
            updated, not_found,
        )
