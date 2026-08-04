"""Auto-categorization engine.

Layered, cheap -> smart. For a given merchant name:
  1. Learned mappings   (what the user has labelled before)   -> handled in main.py via DB
  2. Brand database     (known apps/brands)
  3. Keyword rules      (cafe/restaurant -> Food, etc.)
  4. None               (caller asks the user to label)

Fully on-device-friendly: no network or LLM is involved — categorization is
pure local lookup, so transaction text never leaves the server either. The valid
categories must match the app/dashboard list.
"""
import re

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


async def categorize(merchant: str, raw_sms: str | None = None) -> tuple[str | None, str]:
    """Returns (category, source) from local lookup only — brand DB then keyword
    rules; no network/LLM. Learned mappings are checked in main.py first; an
    unmatched merchant returns (None, "none") so the user is asked to label it.
    Kept async for call-site compatibility. `raw_sms` is currently unused (the
    on-device app already parses); retained for a possible future local heuristic.
    """
    cat = match_brand(merchant)
    if cat:
        return cat, "brand"
    cat = match_keyword(merchant)
    if cat:
        return cat, "keyword"
    return None, "none"
