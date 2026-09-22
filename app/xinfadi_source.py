"""新发地食材参考行情适配器。仅查询指定一天，不生成供应商报价。

python3 xinfadi_source.py 2026-09-07 --output xinfadi-2026-09-07.json
Python 3.9+，无额外依赖；返回值可由比价工具后端直接使用。
"""
import argparse
import datetime as dt
import decimal
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "http://www.xinfadi.com.cn/getPriceData.html"
REFERENCE = "http://www.xinfadi.com.cn/priceDetail.html"


def price(value):
    if value is None or str(value).strip() == "":
        return None
    number = decimal.Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("上游返回无效价格")
    return str(number)


def fetch_prices(day, keyword="", page_size=100, max_pages=20):
    """获取一天的参考行情。空值保持为空；不选定低/均/高价或推定供应商。"""
    day = dt.date.fromisoformat(day).isoformat()
    if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 20:
        raise ValueError("page_size 须在 1–1000，max_pages 须在 1–20")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    rows, seen, total, complete = [], set(), None, False
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode({
            "limit": page_size, "current": page,
            "pubDateStartTime": day.replace("-", "/"),
            "pubDateEndTime": day.replace("-", "/"),
            "prodPcatid": "", "prodCatid": "", "prodName": keyword,
        })
        req = urllib.request.Request(ENDPOINT + "?" + query, data=b"", method="POST")
        with opener.open(req, timeout=20) as response:
            data = json.load(response)
        count = data.get("count")
        batch = data.get("list")
        if not isinstance(count, int) or count < 0 or not isinstance(batch, list):
            raise ValueError("上游响应结构发生变化")
        if total is not None and total != count:
            raise ValueError("分页期间行情总数变化，请稍后重新查询")
        total = count
        for row in batch:
            row_id = row.get("id")
            published = str(row.get("pubDate", ""))
            if row_id is None or row_id in seen or published[:10] != day:
                raise ValueError("上游出现缺失/重复行标识或错误日期")
            seen.add(row_id)
            rows.append({
                "source": "xinfadi", "kind": "market_reference",
                "source_row_id": str(row_id),
                "name": row.get("prodName"), "category": row.get("prodCat"),
                "subcategory": row.get("prodPcat"), "spec": row.get("specInfo"),
                "origin": row.get("place"), "unit": row.get("unitInfo"),
                "low_price": price(row.get("lowPrice")),
                "average_price": price(row.get("avgPrice")),
                "high_price": price(row.get("highPrice")),
                "published_at": published, "reference_url": REFERENCE,
                "supplier": None, "product_url": None,
            })
        if len(rows) == total:
            complete = True
            break
        if not batch or len(rows) > total:
            raise ValueError("上游分页结果与总数不一致")
        if page < max_pages:
            time.sleep(1)
    return {
        "source": "xinfadi", "kind": "market_reference",
        "requested_date": day, "keyword": keyword,
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "reference_url": REFERENCE, "total": total, "returned": len(rows),
        "complete": complete,
        "status": "ok" if complete and rows else "no_data" if complete else "partial",
        "items": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = fetch_prices(args.date, args.keyword)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False))
    else:
        print(encoded)
