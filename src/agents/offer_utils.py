import uuid
from datetime import datetime, timedelta, timezone


def new_offer(price, qty, terms="", ttl_minutes=5):
    now = datetime.now(timezone.utc)
    return {
        "offer_id": uuid.uuid4().hex,
        "price": round(price, 2),
        "qty": qty,
        "terms": terms,
        "expiration": (now + timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
