"""新发地只读行情接口，默认仅监听本机 4196。"""
import datetime as dt
import json
import re
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .xinfadi_source import fetch_prices

CACHE = OrderedDict()
DAY_CACHE = OrderedDict()
LOCK = threading.Lock()
PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>新发地行情查询</title>
<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#18342b}input,button{font:inherit;padding:10px;margin:4px}button{background:#176342;color:white;border:0;border-radius:5px;cursor:pointer}table{border-collapse:collapse;width:100%;margin-top:24px;font-size:14px}th,td{padding:10px;text-align:left;border-bottom:1px solid #ddd}small{color:#64756d}#message{margin-top:20px}form{margin:24px 0}</style>
<h1>新发地行情查询</h1><p>查询食材参考行情，保留最低价、平均价和最高价。</p>
<form><input id="name" required maxlength="80" placeholder="输入品名，如大白菜" aria-label="品名">
<input id="date" type="date" aria-label="行情日期"><button>查询</button><br><small>日期留空：查最近 7 天内最新可用行情；结果会注明实际发布日期。</small></form>
<p id="message"></p><div class="results"><table><thead></thead><tbody></tbody></table></div>
<p><small>这是市场参考价，不是供应商报价。品名相同时仍需核对分类、规格、产地和单位。</small></p>
<p><a href="http://www.xinfadi.com.cn/priceDetail.html" target="_blank" rel="noopener">新发地官网来源</a></p>
<script>
const fields=[['name','品名'],['category','一级分类'],['subcategory','二级分类'],['low_price','最低价'],['average_price','平均价'],['high_price','最高价'],['spec','规格'],['origin','产地'],['unit','单位'],['published_at','发布日期']];
const head=document.createElement('tr');document.querySelector('thead').append(head);for(const [,label] of fields){const th=document.createElement('th');th.textContent=label;head.append(th)}
document.querySelector('form').onsubmit=async e=>{e.preventDefault();const button=document.querySelector('button');button.disabled=true;const message=document.querySelector('#message');message.textContent='正在查询…';document.querySelector('tbody').replaceChildren();try{
 const q=new URLSearchParams({name:document.querySelector('#name').value,date:document.querySelector('#date').value||'latest'});
 const r=await fetch('api/prices?'+q);const d=await r.json();if(!r.ok)throw Error(d.error||'查询失败');
 message.textContent=d.status==='no_data'?'指定范围暂无行情，不以旧价或零价补填。':`行情日期：${d.effective_date} · ${d.returned} 条${d.complete?'':'（结果不完整）'} · ${d.cached?'使用近期查询缓存':'刚从官网获取'}`;
 for(const row of d.items){const tr=document.createElement('tr');for(const [key] of fields){const td=document.createElement('td');td.textContent=row[key]??'—';tr.append(td)}document.querySelector('tbody').append(tr)}
 }catch(e){message.textContent=e.message}finally{button.disabled=false}};
</script></html>'''


STYLE = re.search(r"<style>(.*?)</style>", PAGE, re.S).group(1) + ".results{overflow:auto}"
SCRIPT = re.search(r"<script>(.*?)</script>", PAGE, re.S).group(1)
PAGE = re.sub(r"<style>.*?</style>", '<link rel="stylesheet" href="style.css">', PAGE, flags=re.S)
PAGE = re.sub(r"<script>.*?</script>", '<script src="app.js" defer></script>', PAGE, flags=re.S)


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, value, content_type="application/json; charset=utf-8"):
        raw = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            return self.respond(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/style.css":
            return self.respond(200, STYLE, "text/css; charset=utf-8")
        if url.path == "/app.js":
            return self.respond(200, SCRIPT, "application/javascript; charset=utf-8")
        if url.path == "/health":
            return self.respond(200, {"service": "xinfadi-source", "status": "ok", "version": "20260914.3"})
        if url.path != "/api/prices":
            return self.respond(404, {"error": "接口不存在"})
        try:
            q = parse_qs(url.query, keep_blank_values=True, max_num_fields=5)
            if set(q) - {"name", "date"} or any(len(v) != 1 for v in q.values()):
                raise ValueError("只接受单个 name 和 date 参数")
            result = lookup(q.get("name", [""])[0], q.get("date", ["latest"])[0] or "latest")
            self.respond(200, result)
        except ValueError as e:
            self.respond(400, {"error": str(e)})
        except Exception:
            self.respond(502, {"error": "新发地数据源暂不可用，请稍后重试；本次没有生成替代价格"})


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4196)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
