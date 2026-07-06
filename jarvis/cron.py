"""Translate a human schedule into schtasks arguments, validated BEFORE anything is saved
or installed (spec §5a). Accepts, in order of preference:

    relative delay   "30m", "2h", "90s", "1d"        -> ONCE at now+delta
    interval         "every 2h", "every 15 minutes"  -> MINUTE/HOURLY /MO n
    ISO timestamp    "2026-07-10T09:00"              -> ONCE at that date/time
    5-field cron     "0 9 * * *", "30 8 * * 1,5"      -> DAILY/WEEKLY/HOURLY
    schtasks syntax  "DAILY /ST 09:00", "/SC ONCE .." -> passed through

to_schtasks() returns the schedule fragment as an argv list, or raises ValueError with a
message the assistant can relay. # ponytail: dates are US MM/DD/YYYY (schtasks default on
this box); if the box locale changes, format from the OS short-date pattern instead.
"""
import datetime
import re

_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_PASSTHROUGH = ("MINUTE", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "ONCE", "ONLOGON", "ONIDLE", "ONSTART")
_DOW = {"0": "SUN", "1": "MON", "2": "TUE", "3": "WED", "4": "THU", "5": "FRI", "6": "SAT", "7": "SUN"}


def _once(when):
    return ["/SC", "ONCE", "/SD", when.strftime("%m/%d/%Y"), "/ST", when.strftime("%H:%M")]


def to_schtasks(schedule):
    s = (schedule or "").strip()
    if not s:
        raise ValueError("empty schedule")
    up = s.upper()
    if up.startswith("/SC"):
        return s.split()
    if up.split()[0] in _PASSTHROUGH:
        return ["/SC"] + s.split()

    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|hour|hours|d|day|days)", s, re.I)
    if m:
        unit = _UNIT[m.group(2).lower()[0]]
        return _once(datetime.datetime.now() + datetime.timedelta(**{unit: int(m.group(1))}))

    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()[0]
        return ["/SC", "MINUTE" if unit == "m" else "HOURLY", "/MO", str(n)]

    try:
        return _once(datetime.datetime.fromisoformat(s))
    except ValueError:
        pass

    parts = s.split()
    if len(parts) == 5:
        return _cron_to_schtasks(parts)

    raise ValueError(
        f"unrecognized schedule '{schedule}' — use e.g. '30m', 'every 2h', '0 9 * * *', "
        "an ISO time, or schtasks syntax like 'DAILY /ST 09:00'"
    )


def _cron_to_schtasks(parts):
    """Common 5-field cron -> schtasks. Full cron is broader than schtasks, so unsupported
    shapes raise with a hint rather than silently mistranslating."""
    mins, hrs, dom, mon, dow = parts

    def star(v):
        return v in ("*", "?")

    if not star(dow) and not star(hrs):  # weekly: min hour * * DOW
        days = ",".join(_DOW.get(d, d.upper()[:3]) for d in dow.split(","))
        return ["/SC", "WEEKLY", "/D", days, "/ST", f"{int(hrs):02d}:{int(0 if star(mins) else mins):02d}"]
    if star(dom) and star(mon) and star(dow) and not star(hrs) and not star(mins):  # daily HH:MM
        return ["/SC", "DAILY", "/ST", f"{int(hrs):02d}:{int(mins):02d}"]
    if star(hrs) and not star(mins):  # hourly at minute
        return ["/SC", "HOURLY", "/ST", f"00:{int(mins):02d}"]
    raise ValueError(
        f"cron '{' '.join(parts)}' is too complex for schtasks — use DAILY/WEEKLY/HOURLY schtasks syntax"
    )


if __name__ == "__main__":  # ponytail: one runnable check for the parser
    assert to_schtasks("DAILY /ST 09:00") == ["/SC", "DAILY", "/ST", "09:00"]
    assert to_schtasks("/SC ONCE /ST 10:00") == ["/SC", "ONCE", "/ST", "10:00"]
    assert to_schtasks("every 2h") == ["/SC", "HOURLY", "/MO", "2"]
    assert to_schtasks("every 15 minutes") == ["/SC", "MINUTE", "/MO", "15"]
    assert to_schtasks("0 9 * * *") == ["/SC", "DAILY", "/ST", "09:00"]
    assert to_schtasks("30 8 * * 1,5")[:4] == ["/SC", "WEEKLY", "/D", "MON,FRI"]
    assert to_schtasks("15 * * * *") == ["/SC", "HOURLY", "/ST", "00:15"]
    assert to_schtasks("2026-07-10T09:30")[:2] == ["/SC", "ONCE"]
    assert to_schtasks("30m")[:2] == ["/SC", "ONCE"]
    for bad in ("", "sometime tuesday", "*/5 * * * * *"):
        try:
            to_schtasks(bad); assert False, bad
        except ValueError:
            pass
    print("cron.to_schtasks self-check passed")
