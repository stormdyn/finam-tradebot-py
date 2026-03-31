"""tests/unit/test_contract.py — unit-тесты логики контрактов FORTS."""
import pytest
from datetime import datetime
from core.contract import third_friday, nearest_contract, expiry_day
from core.domain import Symbol


def test_third_friday_june_2026():
    """Третья пятница июня 2026 = 19 июня."""
    assert third_friday(2026, 6) == 19


def test_third_friday_march_2026():
    """Третья пятница марта 2026 = 20 марта."""
    assert third_friday(2026, 3) == 20


def test_nearest_contract_far_from_expiry():
    """1 апреля 2026 → контракт июня 2026."""
    now = datetime(2026, 4, 1)
    sym = nearest_contract("Si", rollover_days=5, now=now)
    assert sym.security_code == "Si-6.26"
    assert sym.security_board == "FORTS"


def test_nearest_contract_rollover():
    """В зоне ролловера (за 3 дня до экспирации) → следующий контракт."""
    # Третья пятница июня 2026 = 19 июня, за 5 дней = 14 июня
    now = datetime(2026, 6, 15)
    sym = nearest_contract("Si", rollover_days=5, now=now)
    assert sym.security_code == "Si-9.26"  # переходим на сентябрь


def test_symbol_str():
    sym = Symbol(security_code="Si-6.26", security_board="FORTS")
    assert str(sym) == "Si-6.26@FORTS"


def test_expiry_day_parsed():
    sym = Symbol(security_code="Si-6.26", security_board="FORTS")
    ed = expiry_day(sym)
    assert ed == 19  # третья пятница июня 2026


def test_expiry_day_unknown():
    sym = Symbol(security_code="UNKNOWN", security_board="FORTS")
    assert expiry_day(sym) == 0
