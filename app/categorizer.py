"""Auto-categorization engine.

Layered, cheap -> smart. For a given merchant name (and optional raw SMS):
  1. Learned mappings   (what the user has labelled before)   -> handled in main.py via DB
  2. Brand database     (known apps/brands)
  3. Keyword rules      (cafe/restaurant -> Food, etc.)
  4. AI fallback        (free Gemini API, if GEMINI_API_KEY is set)
  5. None               (caller asks the user)

The valid categories must match the app/dashboard list.
"""
import os
import re
import json
import httpx

CATEGORIES = [
    "Food", "Grocery", "Shopping", "Transport", "Entertainment",
    "Games", "Personal", "Bills", "Health", "Fuel", "Cash", "Transfer", "Other",
]

# --- Layer 2: brand database (normalized brand fragment -> category) ---
BRAND_DB = {
    # Food delivery / restaurants
    "zomato": "Food", "swiggy": "Food", "eatsure": "Food", "faasos": "Food",
    "behrouz": "Food", "ovenstory": "Food", "dominos": "Food", "pizzahut": "Food",
    "kfc": "Food", "mcdonald": "Food", "burgerking": "Food", "subway": "Food",
    "starbucks": "Food", "chaayos": "Food", "barbeque": "Food", "wow": "Food",
    "haldiram": "Food", "bikanervala": "Food", "theobroma": "Food",
    # Grocery / quick commerce
    "blinkit": "Grocery", "grofers": "Grocery", "zepto": "Grocery",
    "instamart": "Grocery", "bigbasket": "Grocery", "dmart": "Grocery",
    "dunzo": "Grocery", "jiomart": "Grocery", "licious": "Grocery",
    # Shopping
    "amazon": "Shopping", "flipkart": "Shopping", "myntra": "Shopping",
    "ajio": "Shopping", "nykaa": "Shopping", "meesho": "Shopping",
    "tatacliq": "Shopping", "snapdeal": "Shopping", "decathlon": "Shopping",
    "ikea": "Shopping", "croma": "Shopping", "reliancedigital": "Shopping",
    # Transport / ride
    "uber": "Transport", "ola": "Transport", "rapido": "Transport",
    "irctc": "Transport", "redbus": "Transport", "makemytrip": "Transport",
    "goibibo": "Transport", "indigo": "Transport", "vistara": "Transport",
    # Fuel
    "hpcl": "Fuel", "iocl": "Fuel", "bharatpetroleum": "Fuel", "bpcl": "Fuel",
    "indianoil": "Fuel", "shell": "Fuel", "petrol": "Fuel", "fuel": "Fuel",
    # Entertainment / streaming
    "netflix": "Entertainment", "spotify": "Entertainment", "hotstar": "Entertainment",
    "disney": "Entertainment", "jiocinema": "Entertainment", "primevideo": "Entertainment",
    "sonyliv": "Entertainment", "zee5": "Entertainment", "bookmyshow": "Entertainment",
    "pvr": "Entertainment", "inox": "Entertainment", "youtube": "Entertainment",
    # Games / app stores
    "googleplay": "Games", "playstore": "Games", "steam": "Games", "steamgames": "Games",
    "epicgames": "Games", "xbox": "Games", "playstation": "Games", "nintendo": "Games",
    "garena": "Games", "freefire": "Games", "pubg": "Games", "bgmi": "Games",
    "roblox": "Games", "supercell": "Games", "riotgames": "Games",
    # Bills / utilities / telecom
    "jio": "Bills", "airtel": "Bills", "vodafone": "Bills", "vi": "Bills",
    "bsnl": "Bills", "tatapower": "Bills", "adani": "Bills", "electricity": "Bills",
    "mahavitaran": "Bills", "gas": "Bills", "broadband": "Bills", "actfibernet": "Bills",
    # Health
    "pharmeasy": "Health", "1mg": "Health", "netmeds": "Health", "apollo": "Health",
    "practo": "Health", "medplus": "Health", "wellness": "Health", "pharmacy": "Health",
    "cultfit": "Health", "healthkart": "Health",
}

# --- Layer 3: keyword rules (substring in normalized name -> category) ---
KEYWORD_RULES = [
    (("cafe", "restaurant", "resto", "hotel", "kitchen", "foods", "food",
      "bakery", "biryani", "pizza", "burger", "dhaba", "bhojan", "sweets",
      "juice", "tea", "coffee", "dairy", "milk", "canteen", "mess",
      "eatery", "diner", "snacks", "chinese", "tiffin"), "Food"),
    (("mart", "store", "grocery", "kirana", "supermarket", "provision",
      "general stores", "bazaar", "bazar", "vegetables", "fruits"), "Grocery"),
    (("pharma", "medical", "clinic", "hospital", "chemist", "drug",
      "diagnostic", "pathology", "lab", "dental", "care"), "Health"),
    (("petrol", "fuel", "filling", "petroleum", "gas station"), "Fuel"),
    (("travels", "tours", "transport", "cab", "taxi", "auto", "parking",
      "toll", "metro", "railway", "bus"), "Transport"),
    (("game", "gaming", "esports", "playstore", "play store"), "Games"),
    (("cinema", "movies", "multiplex", "theatre", "entertainment"), "Entertainment"),
    (("salon", "spa", "boutique", "fashion", "garments", "footwear",
      "electronics", "mobile", "hardware", "furniture"), "Shopping"),
    (("electricity", "power", "water", "recharge", "broadband", "dth",
      "insurance", "telecom"), "Bills"),
]


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def match_brand(merchant: str) -> str | None:
    norm = normalize(merchant)
    if not norm:
        return None
    if norm in BRAND_DB:
        return BRAND_DB[norm]
    for fragment, category in BRAND_DB.items():
        if fragment in norm:
            return category
    return None


def match_keyword(merchant: str) -> str | None:
    low = (merchant or "").lower()
    for keywords, category in KEYWORD_RULES:
        for kw in keywords:
            if kw in low:
                return category
    return None


async def ai_classify(merchant: str, raw_sms: str | None = None) -> str | None:
    """Free AI fallback via Groq (OpenAI-compatible). No-ops unless GROQ_API_KEY
    is set in .env.

    Get a free key (no card) at https://console.groq.com/keys and put it in .env:
        GROQ_API_KEY=gsk_your_key_here
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None

    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    url = "https://api.groq.com/openai/v1/chat/completions"
    prompt = (
        "You categorize an Indian UPI/bank transaction into exactly ONE category.\n"
        f"Categories: {', '.join(CATEGORIES)}.\n"
        f'Merchant/payee: "{merchant}"\n'
        + (f'SMS: "{raw_sms}"\n' if raw_sms else "")
        + "Reply with ONLY the single category word from the list, nothing else."
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 8,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            for c in CATEGORIES:
                if c.lower() in text.lower():
                    return c
    except Exception:
        return None
    return None


async def categorize(merchant: str, raw_sms: str | None = None) -> tuple[str | None, str]:
    """Returns (category, source). Learned mappings are checked in main.py first."""
    cat = match_brand(merchant)
    if cat:
        return cat, "brand"
    cat = match_keyword(merchant)
    if cat:
        return cat, "keyword"
    cat = await ai_classify(merchant, raw_sms)
    if cat:
        return cat, "ai"
    return None, "none"


async def ai_parse_sms(body: str, sender: str = "") -> dict:
    """AI fallback PARSER for SMS formats the on-device regex can't handle.
    Extracts a completed-debit transaction from a raw bank/UPI SMS. Returns
    {"is_transaction": bool, "amount": float, "merchant": str, "method": str,
     "category": str}. No-ops (is_transaction false) unless GROQ_API_KEY is set.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return {"is_transaction": False}

    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    url = "https://api.groq.com/openai/v1/chat/completions"
    prompt = (
        "You extract a transaction from an Indian bank/UPI SMS.\n"
        "A COMPLETED DEBIT means money LEFT the user's account. It is NOT a debit "
        "if the SMS is an OTP, balance enquiry, a credit/refund/reversal, a DECLINED "
        "or FAILED transaction, a payment REQUEST/collect, a SCHEDULED autopay notice, "
        "or a promo/ad.\n"
        "If and only if it is a completed debit, extract: amount (number), merchant "
        "(payee or merchant name; 'Unknown' if none), method ('card' or 'upi'), and "
        f"category (one of: {', '.join(CATEGORIES)}).\n"
        f'SMS: "{body}"\n'
        'Respond with ONLY compact JSON, no prose: '
        '{"is_transaction": true/false, "amount": number, "merchant": "...", '
        '"method": "upi"/"card", "category": "..."}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(text)
        if not data.get("is_transaction"):
            return {"is_transaction": False}
        amount = float(data.get("amount") or 0)
        if amount <= 0:
            return {"is_transaction": False}
        method = "card" if str(data.get("method", "")).lower() == "card" else "upi"
        category = data.get("category")
        if category not in CATEGORIES:
            category = None
        return {
            "is_transaction": True,
            "amount": amount,
            "merchant": (data.get("merchant") or "Unknown").strip() or "Unknown",
            "method": method,
            "category": category,
        }
    except Exception:
        return {"is_transaction": False}
