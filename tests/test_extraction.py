from sqlalchemy import select

from app.db import session_scope
from app.models import LlmCall
from app.services import llm


def test_extract_wage_simple_range(db_session):
    html = '<p>Compensation: $17.50&#8211;$22.00 per hour</p>'
    result = llm.extract_wage(html, "Stocking Associate")
    parsed = result.parsed
    assert parsed["wage_low"] == 17.5
    assert parsed["wage_high"] == 22.0
    assert parsed["wage_unit"] == "hourly"
    assert 0.0 <= parsed["confidence"] <= 1.0


def test_extract_wage_logs_llm_call(db_session):
    html = "<p>Pay range $18.00 - $20.00 per hour.</p>"
    llm.extract_wage(html, "Lot Associate")
    with session_scope() as s:
        row = s.execute(
            select(LlmCall).where(LlmCall.purpose == "extraction").order_by(LlmCall.id.desc())
        ).scalars().first()
        assert row is not None
        assert row.mocked is True
        assert row.validation_ok is True


def test_classify_role_outdoor(db_session):
    result = llm.classify_role("Lot Associate")
    assert result.parsed["bucket"] == "outdoor"


def test_classify_role_indoor(db_session):
    result = llm.classify_role("Cashier")
    assert result.parsed["bucket"] == "indoor"
