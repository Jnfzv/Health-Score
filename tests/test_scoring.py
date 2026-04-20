"""tests/test_scoring.py — Unit tests for the Customer Health Score logic."""
from __future__ import annotations

import pytest

from scoring.financial import calculate_financial_score
from scoring.support import calculate_support_score
from scoring.health_score import calculate_health_score
from scoring.alerts import generate_alerts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_row() -> dict:
    """Realistic defaults for a healthy, fully-active customer."""
    return {
        # Identity
        "customer_email":      "client@example.com",
        "plan_name":           "Pro",
        "status":              "active",
        # Financial
        "mrr":                 299.0,
        "mrr_trend":           0.0,
        "mrr_declining_months": 0,
        "failure_count_90d":   0,
        "ghost_flag":          False,
        # Engagement
        "days_active_30d":     25,
        "last_login_days":     2,
        "features_adopted":    8,
        "ticket_count_30d":    0,
        # Support
        "open_tickets_48h":    0,
        "has_billing_tickets": False,
        "churn_mention":       False,
        # Context
        "tenure_months":       18.0,
        "end_client_growth":   0.12,
        "utilization_ratio":   0.60,
        "has_addons":          True,
        # Alert fields
        "in_cancel_pipeline":  False,
        "hs_above_85_months":  None,
        "utilization_high_months": None,
    }


# ---------------------------------------------------------------------------
# Financial score tests
# ---------------------------------------------------------------------------

def test_financial_score_active(base_row):
    """Active account with zero payment failures → maximum score."""
    base_row["status"] = "active"
    base_row["failure_count_90d"] = 0
    base_row["ghost_flag"] = False
    score = calculate_financial_score(base_row)
    assert score == 100.0


def test_financial_score_past_due(base_row):
    """Past-due account receives a reduced base score."""
    base_row["status"] = "past_due"
    score = calculate_financial_score(base_row)
    assert score == 30.0


def test_financial_score_ghost(base_row):
    """Ghost flag on an active account caps financial score at paused level (20)."""
    base_row["status"] = "active"
    base_row["ghost_flag"] = True
    score = calculate_financial_score(base_row)
    assert score <= 20.0


# ---------------------------------------------------------------------------
# Support score tests
# ---------------------------------------------------------------------------

def test_support_score_clean(base_row):
    """Zero tickets and no churn mention → perfect support score."""
    base_row["ticket_count_30d"] = 0
    base_row["open_tickets_48h"] = 0
    base_row["has_billing_tickets"] = False
    base_row["churn_mention"] = False
    score = calculate_support_score(base_row)
    assert score == 100.0


def test_support_score_churn_mention(base_row):
    """Churn mention hard-caps the support score at 15."""
    base_row["churn_mention"] = True
    score = calculate_support_score(base_row)
    assert score <= 15.0


# ---------------------------------------------------------------------------
# Health score override tests
# ---------------------------------------------------------------------------

def test_override_unpaid(base_row):
    """Unpaid status triggers the 'unpaid_or_canceled' override cap (≤ 25)."""
    base_row["status"] = "unpaid"
    result = calculate_health_score(base_row)
    assert result["health_score"] <= 25.0


def test_override_ghost_user(base_row):
    """46+ days inactive on an active plan triggers the inactive override (≤ 35)."""
    base_row["last_login_days"] = 46
    base_row["status"] = "active"
    result = calculate_health_score(base_row)
    assert result["health_score"] <= 35.0


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

def test_healthy_classification(base_row):
    """A well-performing account should be classified as 'Healthy'."""
    result = calculate_health_score(base_row)
    assert result["health_score"] >= 75.0
    assert result["category"] == "Healthy"


def test_at_risk_classification(base_row):
    """A deeply troubled account should be classified as 'At Risk'."""
    base_row["status"] = "unpaid"
    base_row["churn_mention"] = True
    base_row["failure_count_90d"] = 5
    base_row["last_login_days"] = 60
    base_row["ticket_count_30d"] = 8
    base_row["has_billing_tickets"] = True
    result = calculate_health_score(base_row)
    assert result["health_score"] < 35.0
    assert result["category"] == "At Risk"


# ---------------------------------------------------------------------------
# Alert tests
# ---------------------------------------------------------------------------

def test_alert_r1_triggered(base_row):
    """churn_mention=True must trigger alert R1 (Churn activo)."""
    base_row["churn_mention"] = True
    alerts = generate_alerts(base_row)
    alert_ids = [a["alert_id"] for a in alerts]
    assert "R1" in alert_ids


def test_alert_o1_triggered(base_row):
    """High engagement, no add-ons, Pro plan → O1 (Upsell addon) fires."""
    base_row["engagement_score"] = 85.0
    base_row["has_addons"] = False
    base_row["plan_name"] = "Pro"
    alerts = generate_alerts(base_row)
    alert_ids = [a["alert_id"] for a in alerts]
    assert "O1" in alert_ids


def test_no_false_alerts(base_row):
    """A perfect account with no risk signals produces an empty alert list."""
    base_row["churn_mention"] = False
    base_row["in_cancel_pipeline"] = False
    base_row["status"] = "active"
    base_row["failure_count_90d"] = 0
    base_row["ghost_flag"] = False
    base_row["last_login_days"] = 1
    base_row["ticket_count_30d"] = 1
    base_row["has_billing_tickets"] = False
    base_row["mrr_trend"] = "stable"
    base_row["end_client_growth"] = 0.05
    base_row["utilization_ratio"] = 0.50
    base_row["has_addons"] = True
    base_row["tenure_months"] = 6.0
    alerts = generate_alerts(base_row)
    assert alerts == []
