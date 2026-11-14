"""SMS composition and delivery.

Safety design
-------------
* The default mode is SIMULATED: messages are rendered and logged, nothing leaves the machine.
* LIVE delivery through Twilio requires ALL of: SMS_MODE=live, SMS_ALLOW_LIVE=true, Twilio credentials in the
  environment, and an explicit `confirm=true` on each send request. Nothing is ever sent automatically.
* Messages are plain ASCII (GSM-7 safe) and capped at 160 characters = one SMS segment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import requests

from ..config import Settings

SMS_LIMIT = 160
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class SmsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _ascii(text: str) -> str:
    return text if text.isascii() else text.encode("ascii", "replace").decode("ascii").replace("?", "-")


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "soon"
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def _fit(variants: list[str]) -> str:
    """First variant that fits one segment; else hard-truncate the shortest."""
    for v in variants:
        v = _ascii(v)
        if len(v) <= SMS_LIMIT:
            return v
    return _ascii(variants[-1])[: SMS_LIMIT - 3].rstrip() + "..."


def compose_shortage_sms(*, clinic_name: str, medicine_short: str, days_to_stockout: float | None,
                         stockout_date: str | None, reorder_qty: int, reorder_by: str | None,
                         risk: str, demand_rising: bool, overdue: bool = False,
                         transfer_from: str | None = None) -> str:
    days = "soon" if days_to_stockout is None else f"{max(0, round(days_to_stockout))} days"
    head = "URGENT" if risk == "critical" else "ALERT"
    rising = " Demand rising." if demand_rising else ""
    order = ("Order now" if overdue else f"Order by {_fmt_date(reorder_by)}") if reorder_qty > 0 else "Review stock"
    qty = f" ({reorder_qty})" if reorder_qty > 0 else ""
    xfer = f" Transfer from {transfer_from} proposed." if transfer_from else ""
    return _fit([
        f"PulseChain {head}: {clinic_name} may run low on {medicine_short} in ~{days} ({_fmt_date(stockout_date)}).{rising} {order}{qty}.{xfer}",
        f"PulseChain {head}: {clinic_name} may run low on {medicine_short} in ~{days}.{rising} {order}{qty}.{xfer}",
        f"PulseChain {head}: {clinic_name} low on {medicine_short} in ~{days}.{rising} {order}{qty}.",
        f"{head}: {clinic_name} low on {medicine_short} in ~{days}. {order}{qty}.",
    ])


def compose_surge_sms(*, clinic_name: str, disease_name: str, ratio_vs_seasonal: float, peak_week: str | None) -> str:
    pct = max(0, round((ratio_vs_seasonal - 1) * 100))
    return _fit([
        f"PulseChain ALERT: {clinic_name} - forecast {disease_name} cases ~{pct}% above seasonal norm in the next weeks"
        f" (peak week of {_fmt_date(peak_week)}). Check stock of related medicines.",
        f"PulseChain ALERT: {clinic_name} - {disease_name} forecast ~{pct}% above normal. Check related medicine stock.",
        f"ALERT: {clinic_name} {disease_name} forecast +{pct}%. Check stock.",
    ])


def validate_phone(number: str) -> str:
    cleaned = re.sub(r"[\s()-]", "", number or "")
    if not _E164.match(cleaned):
        raise SmsError("Phone number must be in E.164 format, e.g. +12025550101")
    return cleaned


@dataclass
class SendResult:
    mode: str
    status: str
    provider_sid: str | None = None
    error: str | None = None


def send_simulated(to_number: str, body: str) -> SendResult:
    return SendResult(mode="simulated", status="simulated")


def send_twilio(settings: Settings, to_number: str, body: str, session: requests.Session | None = None) -> SendResult:
    """Send through Twilio's REST API. Only reachable via `deliver(... mode='live', confirm=True)`."""
    http = session or requests
    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    try:
        resp = http.post(
            url, data={"To": to_number, "From": settings.twilio_from_number, "Body": body},
            auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=10)
    except requests.RequestException as exc:
        return SendResult(mode="live", status="failed", error=f"network error: {exc.__class__.__name__}")
    if resp.status_code >= 300:
        try:
            detail = resp.json().get("message", "")
        except ValueError:
            detail = ""
        return SendResult(mode="live", status="failed", error=f"Twilio HTTP {resp.status_code}: {detail}"[:300])
    try:
        sid = resp.json().get("sid")
    except ValueError:
        sid = None
    return SendResult(mode="live", status="sent", provider_sid=sid)


def deliver(settings: Settings, *, to_number: str, body: str, mode: str, confirm: bool,
            session: requests.Session | None = None) -> SendResult:
    to_number = validate_phone(to_number)
    if not body or len(body) > SMS_LIMIT:
        raise SmsError(f"Message body must be 1-{SMS_LIMIT} characters")
    if mode == "simulated":
        return send_simulated(to_number, body)
    if mode != "live":
        raise SmsError("mode must be 'simulated' or 'live'")
    if not settings.live_sms_possible:
        raise SmsError("Live SMS is disabled: set SMS_MODE=live, SMS_ALLOW_LIVE=true and Twilio credentials in the "
                       "server environment. Use simulated mode for the demo.", status=403)
    if not confirm:
        raise SmsError("Live SMS requires confirm=true on the request", status=400)
    return send_twilio(settings, to_number, body, session=session)
