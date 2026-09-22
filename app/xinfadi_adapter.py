"""Existing 20260914.3 Xinfadi lookup, hosted inside the unified Demo."""
import datetime as dt
import threading,time
from collections import OrderedDict
from xinfadi_source import fetch_prices
CACHE=OrderedDict()
DAY_CACHE=OrderedDict()
LOCK=threading.Lock()

def lookup(name, date="latest"):
    name = name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("请提供 1–80 字的品名")
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    if date != "latest":
        parsed = dt.date.fromisoformat(date)
        if parsed > today:
            raise ValueError("不能查询未来日期")
        date = parsed.isoformat()
    key = (name, date, today.isoformat())
    with LOCK:
        hit = CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            CACHE.move_to_end(key)
            return dict(hit[1], cached=True)
        dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(7)] if date == "latest" else [date]
        for day in dates:
            try:
                # A batch shares the same published daily catalogue. Fetch it
                # once, then match names locally instead of re-requesting up
                # to seven days for every row in an uploaded order.
                daily = DAY_CACHE.get(day)
                reused = bool(daily and daily[0] > time.monotonic())
                if not reused:
                    full = fetch_prices(day, "", page_size=1000, max_pages=20)
                    if not full["complete"]:
                        raise RuntimeError("当天行情未完整取得，暂不能判断品项缺失")
                    DAY_CACHE[day] = (time.monotonic() + 300, full)
                    while len(DAY_CACHE) > 8:
                        DAY_CACHE.popitem(last=False)
                else:
                    full = daily[1]
                    DAY_CACHE.move_to_end(day)
                matches = [row for row in full["items"] if name.casefold() in str(row.get("name") or "").casefold()]
                result = dict(full, keyword=name, items=matches, total=len(matches), returned=len(matches),
                              status="ok" if matches else "no_data", match_method="name_contains", catalogue_cached=reused)
            except ValueError as e:
                raise RuntimeError("上游数据格式发生变化") from e
            if result["items"]:
                break
        result.update(requested_date=date, effective_date=day if result["items"] else None,
                      searched_dates=dates[:dates.index(day)+1], cached=False)
        CACHE[key] = (time.monotonic() + (300 if result["items"] else 30), result)
        while len(CACHE) > 128:
            CACHE.popitem(last=False)
        return result
