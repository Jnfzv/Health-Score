"""
Quick validation script — run before building further.
Requires a valid STRIPE_API_KEY in .env.

Usage:
    python test_stripe.py
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("test_stripe")


def main() -> None:
    # ------------------------------------------------------------------ #
    # 1. Verify the key is present before touching the network            #
    # ------------------------------------------------------------------ #
    api_key = os.getenv("STRIPE_API_KEY", "")
    if not api_key or api_key.startswith("sk_live_your"):
        raise EnvironmentError(
            "STRIPE_API_KEY is missing or still set to the placeholder value.\n"
            "Copy .env.example → .env and fill in your real key."
        )
    logger.info("API key loaded (prefix: %s…)", api_key[:12])

    # ------------------------------------------------------------------ #
    # 2. Instantiate                                                       #
    # ------------------------------------------------------------------ #
    from data_sources.stripe_client import StripeDataExtractor
    extractor = StripeDataExtractor()

    # ------------------------------------------------------------------ #
    # 3. Pull subscriptions — cap at 10 rows for the smoke test           #
    # ------------------------------------------------------------------ #
    logger.info("Fetching subscriptions…")
    subs_df = extractor.get_all_active_subscriptions()

    if subs_df.empty:
        logger.warning("No subscriptions returned. Check that the account has data.")
        return

    preview = subs_df.head(10)

    # ------------------------------------------------------------------ #
    # 4. Print                                                             #
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 70)
    print(f"SUBSCRIPTIONS  ({len(subs_df)} total, showing first {len(preview)})")
    print("=" * 70)

    display_cols = [
        "subscription_id", "customer_email", "status",
        "plan_name", "mrr", "cancel_at_period_end",
    ]
    available = [c for c in display_cols if c in preview.columns]
    print(preview[available].to_string(index=False))

    print("\nColumn dtypes:")
    print(subs_df.dtypes.to_string())

    # ------------------------------------------------------------------ #
    # 5. Payment failures for those 10 customers                          #
    # ------------------------------------------------------------------ #
    customer_ids = preview["customer_id"].dropna().unique().tolist()
    logger.info("Fetching payment failures for %d customer(s)…", len(customer_ids))

    failures_df = extractor.get_payment_failures(customer_ids, days=90)

    print("\n" + "=" * 70)
    print("PAYMENT FAILURES (last 90 days)")
    print("=" * 70)
    print(failures_df.to_string(index=False))

    has_failures = failures_df[failures_df["failure_count"] > 0]
    print(f"\n{len(has_failures)}/{len(failures_df)} customers have ≥1 failure.")


if __name__ == "__main__":
    main()
