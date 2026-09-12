#!/usr/bin/env python3
"""
Crownstitch Pulse Dashboard refresh.
Runs entirely on GitHub Actions' own infrastructure on a schedule.
Fetches Shopify / Klaviyo / Meta Ads / Gmail data, computes a profit estimate,
diffs against the previous data.json for urgent-alert conditions, writes the
new data.json, and (if warranted) emails an action-needed alert.
The workflow (.github/workflows/refresh.yml) is responsible for committing
the resulting data.json back to the repo.
"""
import os
import json
import smtplib
import imaplib
import email
import email.utils
import re
import ssl
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import decode_header

import requests

# ---------------------------------------------------------------------------
# Config / secrets (all provided as environment variables by the workflow)
# ---------------------------------------------------------------------------
SHOPIFY_STORE_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]
SHOPIFY_ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"]
KLAVIYO_API_KEY = os.environ["KLAVIYO_API_KEY"]
META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
META_AD_ACCOUNT_ID = os.environ["META_AD_ACCOUNT_ID"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_EMAILS = [e.strip() for e in os.environ["ALERT_EMAILS"].split(",") if e.strip()]

DATA_JSON_PATH = os.environ.get("DATA_JSON_PATH", "data.json")
SHOPIFY_API_VERSION = "2026-07"
META_API_VERSION = "v26.0"

now = datetime.now(timezone.utc)
NOW_ISO = now.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[refresh] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Step 1: previous snapshot
# ---------------------------------------------------------------------------
def load_previous():
    if not os.path.exists(DATA_JSON_PATH):
        log("No previous data.json found — treating as first run.")
        return {}
    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Shopify helpers
# ---------------------------------------------------------------------------
SHOPIFY_GRAPHQL_URL = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
SHOPIFY_HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
}


def shopify_graphql(query, variables=None):
    resp = requests.post(
        SHOPIFY_GRAPHQL_URL,
        headers=SHOPIFY_HEADERS,
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        log(f"Shopify GraphQL errors: {data['errors']}")
    return data.get("data", {})


def shopify_shopifyql(q):
    data = shopify_graphql(
        "query($q: String!) { shopifyqlQuery(query: $q) { parseErrors "
        "tableData { columns { name } rows } } }",
        {"q": q},
    )
    node = (data or {}).get("shopifyqlQuery") or {}
    if node.get("parseErrors"):
        log(f"ShopifyQL parse errors for '{q}': {node['parseErrors']}")
        return None
    table = node.get("tableData")
    if table is not None:
        table = dict(table)
        table["rowData"] = table.get("rows") or []
    return table


def fetch_shopify(prev_shopify, prev_traffic_sources):
    result = {}

    # --- sessions / conversion rate (best-effort; needs read_reports+read_analytics) ---
    sessions_30d = prev_shopify.get("sessions_30d", 0)
    conversion_rate_30d = prev_shopify.get("conversion_rate_30d", 0)
    traffic_sources = prev_traffic_sources or []
    try:
        table = shopify_shopifyql(
            "FROM sessions SHOW sessions, conversion_rate SINCE -30d UNTIL today"
        )
        if table and table.get("rowData"):
            row = table["rowData"][0]
            sessions_30d = int(float(row["sessions"]))
            conversion_rate_30d = float(row["conversion_rate"])
    except Exception as e:
        log(f"Sessions/conversion query failed, carrying forward: {e}")

    try:
        table = shopify_shopifyql(
            "FROM sessions SHOW sessions GROUP BY referrer_source SINCE -30d UNTIL today"
        )
        if table and table.get("rowData"):
            traffic_sources = [
                {"source": r["referrer_source"], "sessions": int(float(r["sessions"]))}
                for r in table["rowData"]
            ]
    except Exception as e:
        log(f"Traffic-source query failed, carrying forward: {e}")

    result["sessions_30d"] = sessions_30d
    result["conversion_rate_30d"] = conversion_rate_30d
    result["_traffic_sources"] = traffic_sources

    # --- orders_30d / revenue_30d ---
    orders_30d, revenue_30d = 0, 0.0
    try:
        table = shopify_shopifyql("FROM sales SHOW orders, total_sales SINCE -30d UNTIL today")
        if table and table.get("rowData"):
            row = table["rowData"][0]
            orders_30d = int(float(row["orders"]))
            revenue_30d = float(row["total_sales"])
    except Exception as e:
        log(f"Orders/revenue query failed: {e}")
    result["_orders_30d"] = orders_30d
    result["_revenue_30d"] = revenue_30d

    # --- orders_alltime (count via REST, paginated) ---
    orders_alltime = 0
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders/count.json?status=any"
    try:
        r = requests.get(url, headers=SHOPIFY_HEADERS, timeout=30)
        r.raise_for_status()
        orders_alltime = r.json().get("count", 0)
    except Exception as e:
        log(f"orders/count failed: {e}")
    result["orders_alltime"] = orders_alltime

    # --- products ---
    prev_products_by_title = {p["title"]: p for p in prev_shopify.get("products", [])}
    products = []
    data = shopify_graphql(
        """
        query {
          products(first: 50, query: "status:active") {
            edges { node {
              title
              priceRangeV2 { minVariantPrice { amount } }
              variantsCount { count }
              status
              totalInventory
            } }
          }
        }
        """
    )
    for edge in (data.get("products", {}) or {}).get("edges", []):
        node = edge["node"]
        raw_title = node["title"]
        # match against previous snapshot by prefix so titles stay stable
        # even if the Shopify title grows extra " | ..." suffixes
        matched_title = raw_title
        cogs = None
        for prev_title in prev_products_by_title:
            if raw_title.startswith(prev_title) or prev_title.startswith(raw_title):
                matched_title = prev_title
                cogs = prev_products_by_title[prev_title].get("cogs")
                break
        products.append({
            "title": matched_title,
            "price": float(node["priceRangeV2"]["minVariantPrice"]["amount"]),
            "cogs": cogs,
            "variants": node["variantsCount"]["count"],
            "status": node["status"].capitalize(),
            "stock": "In stock (dropship)" if node.get("totalInventory", 0) and node["totalInventory"] > 1000
                     else prev_products_by_title.get(matched_title, {}).get("stock", "In stock (dropship)"),
        })
    result["products"] = products

    # --- discount code status ---
    discount_active = prev_shopify.get("discount_active", True)
    try:
        data = shopify_graphql(
            'query { codeDiscountNodeByCode(code: "WELCOME15") { codeDiscount { '
            '... on DiscountCodeBasic { status } } } }'
        )
        node = data.get("codeDiscountNodeByCode")
        if node and node.get("codeDiscount"):
            discount_active = node["codeDiscount"]["status"] == "ACTIVE"
    except Exception as e:
        log(f"Discount status check failed, carrying forward: {e}")
    result["discount_code"] = "WELCOME15"
    result["discount_active"] = discount_active

    # --- discount redemptions ---
    redemptions = 0
    after = None
    try:
        while True:
            data = shopify_graphql(
                """
                query($after: String) {
                  orders(first: 50, after: $after, query: "discount_code:WELCOME15") {
                    edges { node { id } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                {"after": after},
            )
            edges = data.get("orders", {}).get("edges", [])
            redemptions += len(edges)
            page_info = data.get("orders", {}).get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
    except Exception as e:
        log(f"Discount redemption count failed: {e}")
    result["discount_redemptions_alltime"] = redemptions

    # --- refunds ---
    refunded_orders = []
    total_refunded = 0.0
    try:
        data = shopify_graphql(
            """
            query {
              orders(first: 50, query: "financial_status:refunded OR financial_status:partially_refunded") {
                edges { node {
                  name
                  totalRefundedSet { shopMoney { amount } }
                  refunds { createdAt }
                } }
              }
            }
            """
        )
        for edge in data.get("orders", {}).get("edges", []):
            node = edge["node"]
            amount = float(node["totalRefundedSet"]["shopMoney"]["amount"])
            total_refunded += amount
            refund_dates = [r["createdAt"] for r in node.get("refunds", [])]
            latest = max(refund_dates) if refund_dates else None
            refunded_orders.append({
                "order_name": node["name"],
                "amount": amount,
                "refunded_at": latest,
            })
        refunded_orders.sort(key=lambda o: o["refunded_at"] or "", reverse=True)
    except Exception as e:
        log(f"Refunds query failed: {e}")
    result["refunds"] = {
        "total_refunded_alltime": round(total_refunded, 2),
        "refunded_orders": refunded_orders[:10],
    }

    # --- stuck fulfillment ---
    stuck = []
    try:
        data = shopify_graphql(
            """
            query {
              orders(first: 50, query: "fulfillment_status:unfulfilled") {
                edges { node { name createdAt } }
              }
            }
            """
        )
        for edge in data.get("orders", {}).get("edges", []):
            node = edge["node"]
            created = datetime.strptime(node["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            days_open = (now - created).days
            if days_open > 3:
                stuck.append({"order_name": node["name"], "days_open": days_open})
    except Exception as e:
        log(f"Stuck fulfillment query failed: {e}")
    result["stuck_fulfillment_orders"] = stuck

    result["currency"] = "CAD"
    return result


# ---------------------------------------------------------------------------
# Klaviyo
# ---------------------------------------------------------------------------
KLAVIYO_HEADERS = {
    "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
    "revision": "2025-07-15",
    "accept": "application/json",
}


def fetch_klaviyo(prev_klaviyo):
    flows_by_name = {f["name"]: f for f in prev_klaviyo.get("flows", [])}
    result_flows = []
    try:
        r = requests.get(
            "https://a.klaviyo.com/api/flows/?fields[flow]=name,status",
            headers=KLAVIYO_HEADERS, timeout=30,
        )
        r.raise_for_status()
        flows = r.json().get("data", [])
    except Exception as e:
        log(f"Klaviyo flows fetch failed: {e}")
        return {"flows": list(flows_by_name.values())}

    # figure out the conversion metric id for "Placed Order"
    conversion_metric_id = None
    try:
        r = requests.get(
            "https://a.klaviyo.com/api/metrics/?fields[metric]=name",
            headers=KLAVIYO_HEADERS, timeout=30,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            if m["attributes"]["name"] == "Placed Order":
                conversion_metric_id = m["id"]
                break
    except Exception as e:
        log(f"Klaviyo metrics fetch failed: {e}")

    for f in flows:
        name = f["attributes"]["name"]
        status = f["attributes"]["status"]
        matched_prev_name = name
        note = ""
        for prev_name, prev_flow in flows_by_name.items():
            if name.startswith(prev_name) or prev_name.startswith(name):
                matched_prev_name = prev_name
                note = prev_flow.get("note", "")
                break
        recipients_30d, revenue_30d = 0, 0
        if conversion_metric_id:
            try:
                r = requests.post(
                    "https://a.klaviyo.com/api/flow-values-reports/",
                    headers={**KLAVIYO_HEADERS, "content-type": "application/json"},
                    json={
                        "data": {
                            "type": "flow-values-report",
                            "attributes": {
                                "timeframe": {"key": "last_30_days"},
                                "conversion_metric_id": conversion_metric_id,
                                "statistics": ["recipients"],
                                "value_statistics": ["conversion_value"],
                                "filter": f"equals(flow_id,\"{f['id']}\")",
                            },
                        }
                    },
                    timeout=30,
                )
                if r.ok:
                    rows = r.json().get("data", {}).get("attributes", {}).get("results", [])
                    if rows:
                        stats = rows[0].get("statistics", {})
                        recipients_30d = int(stats.get("recipients", 0) or 0)
                        revenue_30d = round(float(stats.get("conversion_value", 0) or 0), 2)
            except Exception as e:
                log(f"Klaviyo flow report failed for {name}: {e}")
        result_flows.append({
            "name": matched_prev_name,
            "status": status,
            "recipients_30d": recipients_30d,
            "revenue_30d": revenue_30d,
            "note": note,
        })
    return {"flows": result_flows}


# ---------------------------------------------------------------------------
# Meta Ads
# ---------------------------------------------------------------------------
def meta_get(path, params):
    params = {**params, "access_token": META_ACCESS_TOKEN}
    r = requests.get(f"https://graph.facebook.com/{META_API_VERSION}/{path}", params=params, timeout=30)
    return r


# Human-readable labels for Meta's conversion-event / optimization-goal enums,
# used so the dashboard shows "Purchase" / "View content" instead of raw
# API constants like PURCHASE / VIEW_CONTENT.
CUSTOM_EVENT_LABELS = {
    "PURCHASE": "Purchase",
    "VIEW_CONTENT": "View content",
    "ADD_TO_CART": "Add to cart",
    "INITIATE_CHECKOUT": "Initiate checkout",
    "ADD_PAYMENT_INFO": "Add payment info",
    "COMPLETE_REGISTRATION": "Complete registration",
    "LEAD": "Lead",
    "SEARCH": "Search",
    "ADD_TO_WISHLIST": "Add to wishlist",
    "SUBSCRIBE": "Subscribe",
    "START_TRIAL": "Start trial",
    "CONTACT": "Contact",
}

OPTIMIZATION_GOAL_LABELS = {
    "LANDING_PAGE_VIEWS": "Landing page views",
    "LINK_CLICKS": "Link clicks",
    "IMPRESSIONS": "Impressions",
    "REACH": "Reach",
    "THRUPLAY": "ThruPlay",
    "APP_INSTALLS": "App installs",
    "POST_ENGAGEMENT": "Post engagement",
    "VALUE": "Conversion value",
}


def describe_conversion_event(adset):
    """Derive a human-readable conversion-event label directly from an ad
    set's live optimization_goal / promoted_object, rather than trusting a
    stored value that never gets re-checked against Meta."""
    promoted = adset.get("promoted_object") or {}
    custom_event = promoted.get("custom_event_type")
    if custom_event:
        return CUSTOM_EVENT_LABELS.get(custom_event, custom_event.replace("_", " ").title())
    goal = adset.get("optimization_goal")
    if goal:
        return OPTIMIZATION_GOAL_LABELS.get(goal, goal.replace("_", " ").title())
    return None


def fetch_meta(prev_ads):
    result = dict(prev_ads)  # start as a full carry-forward, overwrite what we can read
    try:
        r = meta_get(f"act_{META_AD_ACCOUNT_ID}/campaigns",
                     {"fields": "name,status,daily_budget"})
        r.raise_for_status()
        campaigns = r.json().get("data", [])
        if campaigns:
            c = campaigns[0]
            result["campaign_name"] = c.get("name", result.get("campaign_name"))
            result["campaign_status"] = c.get("status", result.get("campaign_status")).capitalize()
            if c.get("daily_budget"):
                result["daily_budget"] = round(int(c["daily_budget"]) / 100, 2)
    except Exception as e:
        log(f"Meta campaigns fetch failed, carrying forward: {e}")

    # Ad set + conversion event: resolved dynamically every run from whichever
    # ad set is actually ACTIVE right now, rather than a hardcoded ad set name
    # or a value carried forward forever. Ad sets get duplicated/replaced
    # whenever the conversion event needs to change (Meta locks that field
    # post-publish), so a name-based lookup silently goes stale the moment
    # that happens — this looks at live status instead.
    try:
        r = meta_get(f"act_{META_AD_ACCOUNT_ID}/adsets", {
            "fields": "name,status,effective_status,optimization_goal,promoted_object",
        })
        r.raise_for_status()
        adsets = r.json().get("data", [])
        active_adsets = [a for a in adsets if a.get("effective_status") == "ACTIVE"]
        chosen = active_adsets[0] if active_adsets else None
        if chosen is None and adsets:
            # Nothing currently active (e.g. mid-swap between ad sets) —
            # prefer whichever ad set we were already tracking so status
            # doesn't jump to an unrelated ad set, but don't touch
            # conversion_event since it may not reflect what's coming next.
            by_name = {a.get("name"): a for a in adsets}
            chosen = by_name.get(result.get("ad_set_name")) or adsets[0]
            result["ad_set_name"] = chosen.get("name")
            result["ad_set_status"] = (chosen.get("effective_status") or chosen.get("status") or "").capitalize()
        elif chosen is not None:
            result["ad_set_name"] = chosen.get("name")
            result["ad_set_status"] = chosen.get("effective_status", "").capitalize()
            event_label = describe_conversion_event(chosen)
            if event_label:
                result["conversion_event"] = event_label
    except Exception as e:
        log(f"Meta ad sets fetch failed, carrying forward: {e}")

    ad_spend_30d = None
    try:
        r = meta_get(f"act_{META_AD_ACCOUNT_ID}/insights",
                     {"fields": "spend", "date_preset": "last_30d"})
        r.raise_for_status()
        rows = r.json().get("data", [])
        if rows:
            ad_spend_30d = float(rows[0].get("spend", 0))
    except Exception as e:
        log(f"Meta last-30d insights fetch failed: {e}")

    amount_spent_lifetime = result.get("amount_spent_lifetime", 0)
    try:
        r = meta_get(f"act_{META_AD_ACCOUNT_ID}/insights",
                     {"fields": "spend", "date_preset": "maximum"})
        r.raise_for_status()
        rows = r.json().get("data", [])
        if rows:
            amount_spent_lifetime = float(rows[0].get("spend", 0))
    except Exception as e:
        log(f"Meta lifetime insights fetch failed, carrying forward: {e}")

    if ad_spend_30d is None:
        # fallback per original design: use lifetime as a stand-in
        ad_spend_30d = amount_spent_lifetime
        result["_ad_spend_30d_is_fallback"] = True
    else:
        result["_ad_spend_30d_is_fallback"] = False

    result["_ad_spend_30d"] = ad_spend_30d
    result["currency"] = "CAD"

    prev_lifetime = prev_ads.get("amount_spent_lifetime", 0)
    if amount_spent_lifetime > prev_lifetime:
        result["last_spend_change_at"] = NOW_ISO
    else:
        result["last_spend_change_at"] = prev_ads.get("last_spend_change_at", NOW_ISO)
    result["amount_spent_lifetime"] = round(amount_spent_lifetime, 2)

    # fields that rarely change and aren't fetched here
    for k in ("pixel_name", "pixel_id", "conversion_event", "review_status"):
        result.setdefault(k, prev_ads.get(k))

    return result


# ---------------------------------------------------------------------------
# Gmail (IMAP read + SMTP send via App Password)
# ---------------------------------------------------------------------------
def decode_mime(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def classify_source(sender):
    sender_lower = sender.lower()
    if "shopify" in sender_lower:
        return "Shopify"
    if "klaviyo" in sender_lower:
        return "Klaviyo"
    if "facebook" in sender_lower or "meta" in sender_lower:
        return "Meta"
    if "printful" in sender_lower:
        return "Printful"
    return "Customer"


def fetch_gmail():
    mail_result = {}
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX")

        base_query = 'to:info@crownstitch.store newer_than:30d -from:instagram.com'

        # total count
        typ, data = imap.uid("search", None, "X-GM-RAW", f'"{base_query}"')
        total_ids = data[0].split() if typ == "OK" and data[0] else []
        total_30d = len(total_ids)

        # unread count
        typ, data = imap.uid("search", None, "X-GM-RAW", f'"{base_query} is:unread"')
        unread_ids = data[0].split() if typ == "OK" and data[0] else []
        unread_count = len(unread_ids)

        # newest 10 (uid search returns ascending; take the tail)
        newest_ids = total_ids[-10:] if total_ids else []
        messages = []
        for uid in reversed(newest_ids):
            typ, msg_data = imap.uid("fetch", uid, "(RFC822 FLAGS)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            flags_raw = str(msg_data[0][0])
            msg = email.message_from_bytes(raw)
            subject = decode_mime(msg.get("Subject", ""))
            subject = re.sub(r"^(Re|Fwd):\s*", "", subject, flags=re.IGNORECASE)
            sender = decode_mime(msg.get("From", ""))
            date_hdr = msg.get("Date", "")
            try:
                dt = email.utils.parsedate_to_datetime(date_hdr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                date_iso = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                date_iso = NOW_ISO

            # snippet: prefer text/plain; fall back to text/html with tags stripped
            snippet = ""
            html_fallback = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain" and not snippet:
                        try:
                            snippet = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", errors="replace"
                            )
                        except Exception:
                            pass
                    elif ctype == "text/html" and not html_fallback:
                        try:
                            html_fallback = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", errors="replace"
                            )
                        except Exception:
                            pass
            else:
                try:
                    raw_payload = msg.get_payload(decode=True).decode(
                        msg.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    raw_payload = ""
                if msg.get_content_type() == "text/html":
                    html_fallback = raw_payload
                else:
                    snippet = raw_payload

            if not snippet and html_fallback:
                # strip tags/scripts/styles down to plain text
                text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_fallback)
                text = re.sub(r"(?s)<[^>]+>", " ", text)
                text = re.sub(r"&nbsp;|&#8203;|​|­", " ", text)
                snippet = text

            snippet = " ".join(snippet.split())[:150]
            if len(snippet) == 150:
                cut = snippet.rfind(" ")
                if cut > 0:
                    snippet = snippet[:cut]

            unread = "\\Seen" not in flags_raw

            messages.append({
                "date": date_iso,
                "source": classify_source(sender),
                "subject": subject.strip(),
                "snippet": snippet,
                "unread": unread,
            })

        messages.sort(key=lambda m: m["date"], reverse=True)
        mail_result = {
            "unread_count": unread_count,
            "total_30d": total_30d,
            "messages": messages[:8],
            "last_check_status": "ok",
            "last_check_at": NOW_ISO,
        }
        imap.logout()
    except Exception as e:
        log(f"Gmail fetch failed: {e}")
        mail_result = {"_gmail_unavailable": True}
    return mail_result


def send_alert_email(subject, body):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(ALERT_EMAILS)
    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, ALERT_EMAILS, msg.as_string())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    prev = load_previous()
    prev_shopify = prev.get("shopify", {})
    prev_ads = prev.get("ads", {})
    prev_klaviyo = prev.get("klaviyo", {})
    prev_mail = prev.get("mail", {})
    prev_traffic_sources = prev.get("traffic", {}).get("sessions_by_source_30d", [])

    shopify = fetch_shopify(prev_shopify, prev_traffic_sources)
    klaviyo = fetch_klaviyo(prev_klaviyo)
    ads = fetch_meta(prev_ads)
    mail = fetch_gmail()

    if mail.get("_gmail_unavailable"):
        mail = dict(prev_mail)
        mail["last_check_status"] = "gmail_unavailable"
        mail["last_check_at"] = NOW_ISO

    # ---- profit estimate ----
    cogs_values = [p["cogs"] for p in shopify["products"] if p.get("cogs") is not None]
    blended_cogs = sum(cogs_values) / len(cogs_values) if cogs_values else 0
    orders_30d = shopify.pop("_orders_30d")
    revenue_30d = shopify.pop("_revenue_30d")
    ad_spend_30d = ads.pop("_ad_spend_30d")
    ad_spend_is_fallback = ads.pop("_ad_spend_30d_is_fallback")
    cogs_est_30d = round(orders_30d * blended_cogs, 2)
    payment_fees_est_30d = round(orders_30d * 0.30 + revenue_30d * 0.029, 2)
    net_profit_est_30d = round(revenue_30d - cogs_est_30d - ad_spend_30d - payment_fees_est_30d, 2)
    note = (
        "All figures are estimates for a quick read, not bookkeeping-grade numbers: "
        "COGS is orders × the blended per-hat Printful cost (not itemized per order), "
        "and payment fees assume a standard 2.9% + $0.30 CAD per order."
    )
    if ad_spend_is_fallback:
        note += (", and ad spend uses lifetime spend as a stand-in for last-30-day spend "
                 "since the 30-day figure couldn't be confirmed this run")
    profit = {
        "revenue_30d": round(revenue_30d, 2),
        "orders_30d": orders_30d,
        "cogs_est_30d": cogs_est_30d,
        "ad_spend_30d": round(ad_spend_30d, 2),
        "payment_fees_est_30d": payment_fees_est_30d,
        "net_profit_est_30d": net_profit_est_30d,
        "note": note,
    }

    traffic_sources = shopify.pop("_traffic_sources")
    traffic = {"sessions_by_source_30d": traffic_sources}
    # keep the traffic sources tucked into shopify too, for internal diffing convenience next run
    shopify["_traffic_sources"] = traffic_sources

    new_data = {
        "updated_at": NOW_ISO,
        "shopify": {k: v for k, v in shopify.items() if not k.startswith("_")},
        "traffic": traffic,
        "ads": {k: v for k, v in ads.items() if not k.startswith("_")},
        "klaviyo": klaviyo,
        "profit": profit,
        "mail": mail,
    }

    # ---- urgent-item detection ----
    urgent = []

    if prev_shopify.get("orders_alltime", 0) == 0 and shopify.get("orders_alltime", 0) > 0:
        urgent.append("\U0001F389 Crownstitch just got its first order!")

    prev_mail_keys = {(m.get("subject"), m.get("date")) for m in prev_mail.get("messages", [])}
    for m in mail.get("messages", []):
        if m.get("source") == "Customer" and (m.get("subject"), m.get("date")) not in prev_mail_keys:
            urgent.append(
                f"New email from an unrecognized address (likely a customer): '{m['subject']}' "
                f"— check the inbox panel on Pulse."
            )

    for field in ("campaign_status", "ad_set_status"):
        prev_val = str(prev_ads.get(field, "")).lower()
        new_val = str(ads.get(field, "")).lower()
        if prev_val == "active" and new_val != "active":
            label = "Meta campaign" if field == "campaign_status" else "ad set"
            urgent.append(
                f"Your Meta {label} just went from Active to {ads.get(field)} — "
                f"this stops your ads from delivering."
            )
        if "reject" in new_val or "disapprov" in new_val:
            urgent.append(f"Your Meta {field.replace('_', ' ')} is now '{ads.get(field)}' — needs attention.")

    prev_flows = {f["name"]: f for f in prev_klaviyo.get("flows", [])}
    for f in klaviyo["flows"]:
        prev_f = prev_flows.get(f["name"])
        if prev_f and prev_f.get("status") == "live" and f["status"] != "live":
            urgent.append(f"Klaviyo flow '{f['name']}' is no longer live (now '{f['status']}').")

    prev_refund_names = {o["order_name"] for o in prev_shopify.get("refunds", {}).get("refunded_orders", [])}
    for o in shopify["refunds"]["refunded_orders"]:
        if o["order_name"] not in prev_refund_names:
            urgent.append(
                f"Order {o['order_name']} has a refund of ${o['amount']:.2f} on file — "
                f"check Shopify admin."
            )

    prev_stuck_names = {o["order_name"] for o in prev_shopify.get("stuck_fulfillment_orders", [])}
    for o in shopify["stuck_fulfillment_orders"]:
        if o["order_name"] not in prev_stuck_names:
            urgent.append(
                f"Order {o['order_name']} has been unfulfilled for {o['days_open']} days — "
                f"worth a look in Printful/Shopify."
            )

    try:
        if (str(ads.get("campaign_status", "")).lower() == "active"
                and ads.get("amount_spent_lifetime") == prev_ads.get("amount_spent_lifetime")
                and prev_ads.get("last_spend_change_at")):
            last_change = datetime.strptime(prev_ads["last_spend_change_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if now - last_change > timedelta(hours=24):
                urgent.append(
                    "Meta reports your campaign as Active but spend hasn't moved in over a day "
                    "— delivery may have silently stalled."
                )
    except Exception:
        pass

    try:
        spend_delta = ads.get("amount_spent_lifetime", 0) - prev_ads.get("amount_spent_lifetime", 0)
        if spend_delta > 3 * ads.get("daily_budget", 0) and ads.get("daily_budget", 0) > 0:
            urgent.append(
                f"Meta ad spend jumped by ${spend_delta:.2f} since the last refresh — "
                f"more than 3x the daily budget. Worth checking for a misconfiguration."
            )
    except Exception:
        pass

    if (prev_mail.get("last_check_status") == "gmail_unavailable"
            and mail.get("last_check_status") == "ok"
            and prev_mail.get("last_check_at")):
        try:
            prev_check = datetime.strptime(prev_mail["last_check_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if now - prev_check > timedelta(hours=2):
                urgent.append(
                    "The mail check couldn't reach Gmail for a while and just recovered — "
                    "worth a quick look at the inbox in case something came in during that window."
                )
        except Exception:
            pass

    if urgent:
        categories = ", ".join(u.split(".")[0][:40] for u in urgent[:3])
        subject = f"Crownstitch — action needed: {categories}"
        body_lines = ["Hey,", "", "The Crownstitch Pulse refresh found something worth a look:", ""]
        body_lines += [f"- {u}" for u in urgent]
        body_lines += ["", "Full detail: https://madratbuck.github.io/", "",
                       "Reply to this thread if you want to keep it out of spam filters going forward."]
        try:
            send_alert_email(subject, "\n".join(body_lines))
            log(f"Sent alert email covering {len(urgent)} item(s).")
        except Exception as e:
            log(f"Failed to send alert email: {e}")
    else:
        log("No urgent items this run.")

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(new_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    log("Wrote data.json")


if __name__ == "__main__":
    main()
