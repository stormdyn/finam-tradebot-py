"""src/core/contract.py — Логика выбора контракта и экспирации (аналог core/contract.hpp)"""
from __future__ import annotations
from datetime import datetime, timedelta
from core.domain import Symbol

# Месяц → CME-литера для MOEX ISS (для бэктеста)
MONTH_LETTER = {3: "H", 6: "M", 9: "U", 12: "Z"}


def third_friday(year: int, month: int) -> int:
    """
    Третья пятница месяца (день экспирации FORTS).
    Порт C++ core::third_friday() на Python.
    """
    # 1-е число месяца
    first = datetime(year, month, 1)
    # День недели: 0=Monday, 4=Friday
    weekday = first.weekday()
    # Дней до первой пятницы
    days_to_first_fri = (4 - weekday) % 7
    first_friday = 1 + days_to_first_fri
    # Третья пятница = первая + 14 дней
    return first_friday + 14


def is_quarterly(month: int) -> bool:
    """Проверка: месяц квартальный (март, июнь, сентябрь, декабрь)"""
    return month in (3, 6, 9, 12)


def next_quarterly_month(month: int) -> int:
    """Следующий квартальный месяц"""
    if month < 3:
        return 3
    if month < 6:
        return 6
    if month < 9:
        return 9
    if month < 12:
        return 12
    return 3  # декабрь → март следующего года


def quarter_letter(month: int) -> str:
    """Буква квартала FORTS: 3→H, 6→M, 9→U, 12→Z"""
    return MONTH_LETTER.get(month, "X")


def nearest_contract(
    ticker: str,
    rollover_days: int = 5,
    now: datetime | None = None
) -> Symbol:
    """
    Выбрать ближайший квартальный контракт.
    Порт C++ core::nearest_contract().
    
    Формат: {TICKER}-{MM}.{YY}@FORTS
    Пример: Si, 6, 2026 → Symbol("Si-6.26", "FORTS")
    """
    if now is None:
        now = datetime.utcnow()
    
    cur_year = now.year
    cur_month = now.month
    cur_day = now.day
    
    # Найти ближайший квартальный месяц
    exp_month = cur_month
    exp_year = cur_year
    
    if not is_quarterly(exp_month):
        exp_month = next_quarterly_month(exp_month)
        if exp_month <= cur_month:
            exp_year += 1
    
    # День экспирации (третья пятница)
    exp_day = third_friday(exp_year, exp_month)
    
    # Если в зоне ролловера → перейти к следующему контракту
    if (cur_year == exp_year and cur_month == exp_month and
            cur_day >= exp_day - rollover_days):
        exp_month = next_quarterly_month(exp_month)
        if exp_month <= cur_month:
            exp_year += 1
    
    # Формат: {TICKER}-{MM}.{YY}
    code = f"{ticker}-{exp_month}.{exp_year % 100}"
    return Symbol(security_code=code, security_board="FORTS")


def expiry_day(sym: Symbol) -> int:
    """
    День экспирации по символу.
    Парсит {TICKER}-{MM}.{YY} → третья пятница месяца.
    Возвращает 0 если не удалось распарсить.
    """
    code = sym.security_code
    dash = code.find("-")
    dot = code.find(".")
    
    if dash == -1 or dot == -1:
        return 0
    
    try:
        exp_month = int(code[dash + 1:dot])
        exp_year_short = int(code[dot + 1:])
        # Декода 2020-2029 (достаточно до 2030)
        exp_year = 2020 + exp_year_short
        return third_friday(exp_year, exp_month)
    except (ValueError, IndexError):
        return 0