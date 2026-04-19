from __future__ import annotations

from typing import Callable, NamedTuple

from ._utils import _get

# ---------------------------------------------------------------------------
# Plan metadata used in opportunity checks
# ---------------------------------------------------------------------------

# Plans that have add-ons available for upsell.
_ADDON_ELIGIBLE_PLANS: frozenset[str] = frozenset({"Pro", "App"})

# Plans whose customers qualify as potential evangelists.
_EVANGELIST_PLANS: frozenset[str] = frozenset({"Pro", "App"})

# Severity sort order for the returned list (most urgent first).
_SEVERITY_RANK: dict[str, int] = {
    "CRITICAL":    0,
    "HIGH":        1,
    "MEDIUM":      2,
    "OPPORTUNITY": 3,
    "STRATEGIC":   4,
}


# ---------------------------------------------------------------------------
# Internal rule definition
# ---------------------------------------------------------------------------

class _AlertRule(NamedTuple):
    alert_id:   str
    alert_name: str
    severity:   str
    action:     str
    check:      Callable  # (row) -> bool


# ---------------------------------------------------------------------------
# Individual condition checkers
# ---------------------------------------------------------------------------

def _check_r1(row) -> bool:
    """Churn activo: customer has expressed intent OR a cancel deal exists."""
    return (
        bool(_get(row, "churn_mention",       False))
        or bool(_get(row, "in_cancel_pipeline", False))
    )


def _check_r2(row) -> bool:
    """Churn pasivo: financial distress without explicit churn signal."""
    status   = str(_get(row, "status",            "") or "")
    failures = int(_get(row, "failure_count_90d", 0) or 0)
    ghost    = bool(_get(row, "ghost_flag",        False))
    return status in ("past_due", "unpaid") or failures >= 2 or ghost


def _check_r3(row) -> bool:
    """Ghost user: active subscription, no logins, no tickets — silent churn risk."""
    last_login = int(_get(row, "last_login_days",   0) or 0)
    status     = str(_get(row, "status",            "") or "")
    tickets    = int(_get(row, "ticket_count_30d",  0) or 0)
    return last_login > 21 and status == "active" and tickets == 0


def _check_r4(row) -> bool:
    """
    Deterioro financiero: MRR has been declining for 3+ consecutive months.
    Accepts mrr_trend as a string ('decreasing') or a negative float.
    If mrr_declining_months is absent, fires on the trend alone.
    """
    mrr_trend       = _get(row, "mrr_trend",          None)
    declining_months = _get(row, "mrr_declining_months", None)

    is_declining = mrr_trend == "decreasing" or (
        isinstance(mrr_trend, (int, float)) and float(mrr_trend) < 0
    )
    if not is_declining:
        return False

    if declining_months is not None:
        return int(declining_months) >= 3
    return True  # trend confirmed but no duration data — fire conservatively


def _check_r5(row) -> bool:
    """Concentración soporte: repeated tickets with billing friction."""
    tickets     = int(_get(row, "ticket_count_30d",    0) or 0)
    has_billing = bool(_get(row, "has_billing_tickets", False))
    return tickets >= 3 and has_billing


def _check_o1(row) -> bool:
    """Upsell addon: high engagement, no addons, plan supports them."""
    engagement = float(_get(row, "engagement_score", 0) or 0)
    has_addons = bool(_get(row, "has_addons",         False))
    plan       = str(_get(row, "plan_name",           "") or "")
    return (
        engagement > 70
        and not has_addons
        and plan in _ADDON_ELIGIBLE_PLANS
    )


def _check_o2(row) -> bool:
    """
    Upgrade plan: sustained high utilization OR significant end-client growth.
    Utilization check requires 2+ high months when duration data is available.
    """
    growth      = float(_get(row, "end_client_growth",       0) or 0)
    utilization = _get(row, "utilization_ratio",             None)
    high_months = _get(row, "utilization_high_months",       None)

    if growth > 0.20:
        return True

    if utilization is not None and float(utilization) > 0.85:
        if high_months is not None:
            return int(high_months) >= 2
        return True  # utilization confirmed but no duration data — fire

    return False


def _check_o3(row) -> bool:
    """Sobreuso: customer is consuming more than their plan quota."""
    utilization = _get(row, "utilization_ratio", None)
    if utilization is None:
        return False
    return float(utilization) > 1.0


def _check_o4(row) -> bool:
    """
    Evangelista: stable high health, premium plan, established tenure.
    Requires health_score > 85 for 3+ months when historical data is present.
    """
    health = float(_get(row, "health_score",        0) or 0)
    if health <= 85:
        return False

    plan         = str(_get(row, "plan_name",           "") or "")
    tenure       = float(_get(row, "tenure_months",     0) or 0)
    hs_months    = _get(row, "hs_above_85_months",      None)

    if hs_months is not None and int(hs_months) < 3:
        return False

    return plan in _EVANGELIST_PLANS and tenure > 12


# ---------------------------------------------------------------------------
# Alert catalogue
# ---------------------------------------------------------------------------

_RULES: tuple[_AlertRule, ...] = (
    _AlertRule(
        alert_id="R1",
        alert_name="Churn activo",
        severity="CRITICAL",
        action=(
            "Contactar en < 24 h. Identificar motivo concreto (precio, "
            "funcionalidad, competencia) y escalar a CS senior si procede."
        ),
        check=_check_r1,
    ),
    _AlertRule(
        alert_id="R2",
        alert_name="Churn pasivo",
        severity="CRITICAL",
        action=(
            "Revisar historial de pagos y resolver incidencia de cobro. "
            "Ofrecer prórroga o método de pago alternativo antes de suspender."
        ),
        check=_check_r2,
    ),
    _AlertRule(
        alert_id="R3",
        alert_name="Ghost user",
        severity="HIGH",
        action=(
            "Enviar secuencia de re-engagement personalizada. "
            "Si no hay respuesta en 7 días, llamada de check-in proactiva."
        ),
        check=_check_r3,
    ),
    _AlertRule(
        alert_id="R4",
        alert_name="Deterioro financiero",
        severity="HIGH",
        action=(
            "Analizar causa raíz del descenso de MRR (downgrade, pausas, "
            "cancelaciones parciales) y proponer plan de recuperación."
        ),
        check=_check_r4,
    ),
    _AlertRule(
        alert_id="R5",
        alert_name="Concentración soporte",
        severity="MEDIUM",
        action=(
            "Revisar tickets de facturación abiertos. Programar llamada "
            "de resolución y documentar fricción recurrente para producto."
        ),
        check=_check_r5,
    ),
    _AlertRule(
        alert_id="O1",
        alert_name="Upsell addon",
        severity="OPPORTUNITY",
        action=(
            "Presentar demo del addon más relevante para su caso de uso. "
            "Buen momento: alta adopción indica que aprovechará el valor."
        ),
        check=_check_o1,
    ),
    _AlertRule(
        alert_id="O2",
        alert_name="Upgrade plan",
        severity="OPPORTUNITY",
        action=(
            "Proponer upgrade al siguiente plan antes de que la limitación "
            "genere fricción. Incluir comparativa de ROI en la conversación."
        ),
        check=_check_o2,
    ),
    _AlertRule(
        alert_id="O3",
        alert_name="Sobreuso",
        severity="OPPORTUNITY",
        action=(
            "Notificar el sobreuso antes de que se genere cobro extra. "
            "Convertir la conversación en oportunidad de upgrade de plan."
        ),
        check=_check_o3,
    ),
    _AlertRule(
        alert_id="O4",
        alert_name="Evangelista potencial",
        severity="STRATEGIC",
        action=(
            "Invitar a programa de referidos o caso de éxito. "
            "Explorar co-marketing, testimonial o participación en advisory board."
        ),
        check=_check_o4,
    ),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_alerts(row) -> list[dict]:
    """
    Evaluate all alert rules against a customer row and return every active alert.

    The row may be a plain dict or a pandas Series. Fields that are absent or
    NA fall back to safe defaults inside each checker (no alert fires falsely).

    Alerts are returned sorted by severity: CRITICAL → HIGH → MEDIUM →
    OPPORTUNITY → STRATEGIC. Returns an empty list when no rule is triggered.

    Each alert dict contains:
        alert_id   : str  — R1–R5 (risk) or O1–O4 (opportunity)
        alert_name : str  — short human-readable label
        severity   : str  — CRITICAL | HIGH | MEDIUM | OPPORTUNITY | STRATEGIC
        action     : str  — recommended CS action
    """
    active = [
        {
            "alert_id":   rule.alert_id,
            "alert_name": rule.alert_name,
            "severity":   rule.severity,
            "action":     rule.action,
        }
        for rule in _RULES
        if rule.check(row)
    ]

    active.sort(key=lambda a: _SEVERITY_RANK.get(a["severity"], 99))
    return active
