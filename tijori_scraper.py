import requests
import json
import os
import csv
import difflib
from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL           = "https://www.tijorifinance.com/results/quarterly-results/"
DATA_FILE          = Path("data/tijori_known.json")
STOCKS_CSV         = Path("indianStocks.csv")
FUZZY_THRESHOLD    = 0.75

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
WATCHLIST_RAW      = os.environ.get("WATCHLIST", "ALL")


def load_stock_master():
    master = {}
    if not STOCKS_CSV.exists():
        print("[WARN] indianStocks.csv not found.")
        return master
    with STOCKS_CSV.open(encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3: continue
            name = row[0].strip()
            if not name or name.lower() == "name": continue
            master[name.lower()] = {
                "name": name,
                "bse": row[1].strip() if len(row) > 1 else "",
                "nse": row[2].strip() if len(row) > 2 else "",
                "industry": row[4].strip() if len(row) > 4 else "",
            }
    print(f"[INFO] Loaded {len(master)} stocks")
    return master


def find_stock_info(company_name, master):
    query = company_name.lower().strip()
    if query in master:
        return master[query]
    matches = difflib.get_close_matches(query, list(master.keys()), n=1, cutoff=FUZZY_THRESHOLD)
    if matches:
        return master[matches[0]]
    clean = query.rstrip(". ")
    for k, v in master.items():
        if clean in k or k in clean or (len(clean) >= 8 and k.startswith(clean[:8])):
            return v
    return {}


def build_watchlist():
    raw = WATCHLIST_RAW.strip().upper()
    if not raw or raw == "ALL":
        return set()
    return set(x.strip().upper() for x in WATCHLIST_RAW.split(",") if x.strip())


def is_in_watchlist(stock_info, company_name, watchlist):
    if not watchlist:
        return True
    nse = stock_info.get("nse", "").upper()
    bse = str(stock_info.get("bse", "")).upper()
    name_upper = company_name.upper()
    for item in watchlist:
        if item in (nse, bse) or item in name_upper:
            return True
    return False


# ── Scrape ────────────────────────────────────────────────────────────────────
def fetch_quarterly_results():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    r = requests.get(BASE_URL, headers=headers, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.find_all("div", class_="result_item")

    results = []
    for item in items:
        try:
            company = item.find("h6").get_text(strip=True) if item.find("h6") else ""
            link = item.find("a", href=True)
            detail_link = "https://www.tijorifinance.com" + link["href"] if link and link.get("href") else ""

            date_tag = item.find("span", class_="event_date")
            date_str = date_tag.get_text(strip=True).replace("•", "").strip() if date_tag else ""

            values = item.find_all("span", class_="value")
            mcap = values[0].get_text(strip=True) if len(values) > 0 else "N/A"
            pe = values[1].get_text(strip=True) if len(values) > 1 else "N/A"

            financials = {}
            table = item.find("table", class_="inner-table")
            if table:
                rows = table.find_all("tr")[1:]
                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) < 5: continue
                    metric = cols[0].get_text(strip=True)
                    yoy = cols[1].get_text(strip=True)
                    qoq = cols[2].get_text(strip=True)
                    mar26 = cols[3].get_text(strip=True)
                    dec25 = cols[4].get_text(strip=True)
                    mar25 = cols[5].get_text(strip=True) if len(cols) > 5 else ""

                    financials[metric] = {
                        "yoy": yoy,
                        "qoq": qoq,
                        "mar2026": mar26,
                        "dec2025": dec25,
                        "mar2025": mar25
                    }

            if company and date_str:
                results.append({
                    "company": company,
                    "date": date_str,
                    "mcap": mcap,
                    "pe": pe,
                    "financials": financials,
                    "detail_link": detail_link
                })
        except:
            continue

    print(f"[INFO] Fetched {len(results)} results")
    return results


def make_key(item):
    return f"{item['company']}|{item['date']}"


def load_known():
    if not DATA_FILE.exists():
        return set()
    with DATA_FILE.open(encoding="utf-8") as f:
        return set(json.load(f))


def save_known(keys):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(sorted(list(keys)), f, indent=2)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, data=payload, timeout=20).raise_for_status()
        print("[INFO] Message sent.")
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")


def send_in_batches(lines, header):
    sep = "\n\n─────────────────\n\n"
    batch = header
    for line in lines:
        candidate = batch + (sep if batch != header else "\n\n") + line
        if len(candidate) > 3900:
            send_telegram(batch)
            batch = header + "\n\n" + line
        else:
            batch = candidate
    if batch:
        send_telegram(batch)


# ── Main ──────────────────────────────────────────────────────────────────────
def notify():
    now = datetime.now().strftime("%d %b %Y %I:%M %p IST")
    master = load_stock_master()
    watchlist = build_watchlist()
    current = fetch_quarterly_results()
    known = load_known()

    new_watch = []
    new_keys = set(known)

    for item in current:
        k = make_key(item)
        if k in known:
            continue

        info = find_stock_info(item["company"], master)
        item["nse"] = info.get("nse", "")
        item["bse"] = info.get("bse", "")
        item["industry"] = info.get("industry", "")

        if is_in_watchlist(info, item["company"], watchlist):
            new_watch.append(item)

        new_keys.add(k)

    save_known(new_keys)

    wl_note = " (All Stocks)" if not watchlist else f" (Watchlist: {', '.join(sorted(watchlist))})"

    if not new_watch:
        print("[INFO] No new results today.")
        return

    header = f"📊 <b>New Quarterly Results Published</b>{wl_note}\n🕐 {now}\n📌 {len(new_watch)} new result(s)"

    lines = []
    for item in new_watch:
        sym_parts = []
        if item.get("nse"): sym_parts.append(f'<code>NSE: {item["nse"]}</code>')
        if item.get("bse"): sym_parts.append(f'<code>BSE: {item["bse"]}</code>')
        sym_line = " | ".join(sym_parts) if sym_parts else ""

        line = f"🏢 <b>{item['company']}</b>\n{sym_line}\n🏭 {item.get('industry', 'N/A')}\n"
        line += f"📅 {item['date']}   |   M Cap: {item['mcap']}   |   PE: {item['pe']}\n\n"

        # Compact Table - One row per metric
        line += "Metric            YoY       QoQ     Mar 2026   Dec 2025   Mar 2025\n"
        line += "───────────────────────────────────────────────────────────────\n"

        for metric in ["Sales", "Operating Profit", "Net Profit"]:
            if metric in item["financials"]:
                d = item["financials"][metric]
                line += f"{metric:<16} {d.get('yoy','N/A'):>8}  {d.get('qoq','N/A'):>8}   {d.get('mar2026','N/A'):>8}   {d.get('dec2025','N/A'):>8}   {d.get('mar2025','N/A'):>8}\n"

        line += f'\n🔗 <a href="{item["detail_link"]}">View Detailed Financials →</a>'
        lines.append(line)

    send_in_batches(lines, header)


if __name__ == "__main__":
    notify()
