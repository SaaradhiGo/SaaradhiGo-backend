"""Dates in the market's own style (15 Nov, US polish round).

The USA writes the month first ("Sat, Nov 14", "Nov 9 – 15", "Nov 14, 2026", "7:30 PM"); India
keeps the day first ("Sat, 14 Nov", "9 – 15 Nov", "14 Nov 2026", "19:30"). Built with f-strings,
never `%-d`, so the same code runs on every platform.
"""
from datetime import datetime


def is_us(country):
    return str(country or '').upper() == 'US'


def _d(value):
    return value.date() if isinstance(value, datetime) else value


def day_month(value, country):
    """"Nov 14" or "14 Nov"."""
    d = _d(value)
    return f'{d:%b} {d.day}' if is_us(country) else f'{d.day} {d:%b}'


def weekday_date(value, country):
    """"Sat, Nov 14" or "Sat, 14 Nov"."""
    d = _d(value)
    return f'{d:%a}, {day_month(d, country)}'


def full_date(value, country):
    """"Nov 14, 2026" or "14 Nov 2026"."""
    d = _d(value)
    return f'{d:%b} {d.day}, {d.year}' if is_us(country) else f'{d.day} {d:%b} {d.year}'


def clock(value, country):
    """"7:30 PM" or "19:30"."""
    if is_us(country):
        hour = value.hour % 12 or 12
        return f'{hour}:{value:%M} {"AM" if value.hour < 12 else "PM"}'
    return f'{value:%H:%M}'


def date_time(value, country):
    """"Nov 14, 2026, 7:30 PM" or "14 Nov 2026, 19:30"."""
    return f'{full_date(value, country)}, {clock(value, country)}'


def date_range(first, last, country):
    """"Nov 9 – 15" or "9 – 15 Nov" within a month; both ends in full across months."""
    first, last = _d(first), _d(last)
    if first == last:
        return day_month(first, country)
    if (first.year, first.month) == (last.year, last.month):
        return f'{first:%b} {first.day} – {last.day}' if is_us(country) else f'{first.day} – {last.day} {first:%b}'
    return f'{day_month(first, country)} – {day_month(last, country)}'


__all__ = ['clock', 'date_range', 'date_time', 'day_month', 'full_date', 'is_us', 'weekday_date']
