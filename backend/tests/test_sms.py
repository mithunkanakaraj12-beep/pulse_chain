from unittest.mock import MagicMock

import pytest

from pulsechain.services import sms
from conftest import make_settings


def test_shortage_sms_fits_one_segment_and_is_ascii_for_long_names():
    body = sms.compose_shortage_sms(
        clinic_name="Extraordinarily Long Named Rural Community Health Sub-Centre of the Northern Plateau",
        medicine_short="IV Ringer's Lactate", days_to_stockout=12.4, stockout_date="2026-06-06", reorder_qty=480,
        reorder_by="2026-05-30", risk="critical", demand_rising=True, transfer_from="Another Long Named PHC")
    assert len(body) <= sms.SMS_LIMIT and body.isascii()
    assert "12" in body


def test_shortage_sms_contains_clinic_medicine_timeline_and_action():
    body = sms.compose_shortage_sms(clinic_name="Clinic A", medicine_short="malaria treatment (ACT)", days_to_stockout=12.0,
                                    stockout_date="2026-06-06", reorder_qty=480, reorder_by="2026-05-30", risk="high", demand_rising=True)
    assert "Clinic A" in body and "malaria treatment" in body and "12 days" in body and "Demand rising" in body and "480" in body


def test_non_ascii_names_are_made_gsm_safe():
    body = sms.compose_shortage_sms(clinic_name="Clínica Niño", medicine_short="ORS", days_to_stockout=5, stockout_date=None,
                                    reorder_qty=0, reorder_by=None, risk="high", demand_rising=False)
    assert body.isascii()


def test_phone_validation():
    assert sms.validate_phone("+1 (202) 555-0101") == "+12025550101"
    for bad in ("2025550101", "+0123456", "abc", ""):
        with pytest.raises(sms.SmsError):
            sms.validate_phone(bad)


def test_simulated_mode_never_touches_the_network(tmp_path):
    s = make_settings(tmp_path)
    session = MagicMock()
    res = sms.deliver(s, to_number="+12025550101", body="hello", mode="simulated", confirm=False, session=session)
    assert res.status == "simulated" and res.mode == "simulated"
    session.post.assert_not_called()


def test_live_mode_is_blocked_without_server_opt_in(tmp_path):
    s = make_settings(tmp_path, twilio_account_sid="AC1", twilio_auth_token="t", twilio_from_number="+15005550006")
    with pytest.raises(sms.SmsError) as exc:
        sms.deliver(s, to_number="+12025550101", body="hi", mode="live", confirm=True, session=MagicMock())
    assert exc.value.status == 403


def test_live_mode_requires_explicit_confirm(tmp_path):
    s = make_settings(tmp_path, sms_mode="live", sms_allow_live=True, twilio_account_sid="AC1", twilio_auth_token="t", twilio_from_number="+15005550006")
    session = MagicMock()
    with pytest.raises(sms.SmsError):
        sms.deliver(s, to_number="+12025550101", body="hi", mode="live", confirm=False, session=session)
    session.post.assert_not_called()


def test_live_mode_calls_twilio_when_fully_enabled_and_confirmed(tmp_path):
    s = make_settings(tmp_path, sms_mode="live", sms_allow_live=True, twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from_number="+15005550006")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=201, json=lambda: {"sid": "SM123"})
    res = sms.deliver(s, to_number="+12025550101", body="hi", mode="live", confirm=True, session=session)
    assert res.status == "sent" and res.provider_sid == "SM123"
    args, kwargs = session.post.call_args
    assert "Accounts/AC1/Messages.json" in args[0] and kwargs["auth"] == ("AC1", "tok") and kwargs["data"]["To"] == "+12025550101"


def test_live_failure_is_reported_not_raised(tmp_path):
    s = make_settings(tmp_path, sms_mode="live", sms_allow_live=True, twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from_number="+15005550006")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=401, json=lambda: {"message": "Authenticate"})
    res = sms.deliver(s, to_number="+12025550101", body="hi", mode="live", confirm=True, session=session)
    assert res.status == "failed" and "401" in res.error


def test_body_length_guard(tmp_path):
    with pytest.raises(sms.SmsError):
        sms.deliver(make_settings(tmp_path), to_number="+12025550101", body="x" * 161, mode="simulated", confirm=False)
