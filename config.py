"""Institutions and futures contracts tracked by the dashboard."""

import os

# SEC requires a descriptive User-Agent with contact info on every request.
# Overridable via env var so a public/CI deployment can supply its own contact
# instead of committing a personal address to the repo.
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "Sahar Cohen sahar.cohen@bay.security"
)

# How many quarterly report periods to keep (5 = one year of tables + the
# prior quarter needed to compute the oldest quarter's QoQ change).
QUARTERS = 5

# Each institution is a *group* of CIKs. Large managers split their 13F
# reporting across affiliated entities and re-organise over time, so the
# holdings for one brand can move between CIKs mid-year. We union them.
INSTITUTIONS = [
    {
        "id": "blackrock",
        "name_en": "BlackRock",
        "name_he": "בלאקרוק",
        "kind": "asset_manager",
        "ciks": [
            2012383,   # BlackRock, Inc. (current filer, 2024-Q3 onward)
            1364742,   # BlackRock Finance, Inc. (former filer, through 2024-Q2)
        ],
    },
    {
        "id": "vanguard",
        "name_en": "The Vanguard Group",
        "name_he": "ונגארד",
        "kind": "asset_manager",
        # Vanguard filed as a single manager through 2025-Q3, then moved to a
        # 13F-NT notice at the parent and split holdings across subsidiaries.
        "ciks": [
            102909,    # Vanguard Group Inc (through 2025-Q4; 13F-NT after)
            2100121,   # Vanguard Portfolio Management LLC
            2100119,   # Vanguard Capital Management LLC
            933478,    # Vanguard Fiduciary Trust Co
            1767306,   # Vanguard Personalized Indexing Management LLC
        ],
    },
    {
        "id": "statestreet",
        "name_en": "State Street",
        "name_he": "סטייט סטריט",
        "kind": "asset_manager",
        "ciks": [93751],
    },
    {
        "id": "jpmorgan",
        "name_en": "JPMorgan Chase",
        "name_he": "ג'יי פי מורגן צ'ייס",
        "kind": "bank",
        "ciks": [19617],
    },
    {
        "id": "bofa",
        "name_en": "Bank of America",
        "name_he": "בנק אוף אמריקה",
        "kind": "bank",
        "ciks": [70858],
    },
    {
        "id": "berkshire",
        "name_en": "Berkshire Hathaway",
        "name_he": "ברקשייר האת'וויי",
        "kind": "insurer",
        "ciks": [1067983],
    },
    {
        "id": "metlife",
        "name_en": "MetLife",
        "name_he": "מטלייף",
        "kind": "insurer",
        # The parent's own 13F is a token ~$13M in ETFs; the general-account
        # equity book is reported by the asset-management subsidiary.
        "ciks": [
            1529735,   # MetLife Investment Management, LLC
            1099219,   # MetLife Inc
        ],
    },
]

# CFTC Commitments of Traders — legacy futures-only report.
# Codes verified against the live dataset; names are the CFTC's own.
COT_DATASET = "6dca-aqww"
COT_WEEKS = 53  # one year of weekly reports

COT_CONTRACTS = [
    # code,     Hebrew label,            group
    ("13874A", "S&P 500 (E-mini)",       "מדדי מניות"),
    ("20974+", "נאסד\"ק 100",             "מדדי מניות"),
    ("239742", "ראסל 2000 (E-mini)",     "מדדי מניות"),
    ("12460+", "דאו ג'ונס",               "מדדי מניות"),
    ("1170E1", "VIX",                    "מדדי מניות"),
    ("020601", "אג\"ח ל-30 שנה",          "ריבית"),
    ("020604", "Ultra Bond",             "ריבית"),
    ("043602", "אג\"ח ל-10 שנים",         "ריבית"),
    ("044601", "אג\"ח ל-5 שנים",          "ריבית"),
    ("042601", "אג\"ח לשנתיים",           "ריבית"),
    ("088691", "זהב",                    "סחורות"),
    ("084691", "כסף",                    "סחורות"),
    ("085692", "נחושת",                  "סחורות"),
    ("067651", "נפט WTI",                "סחורות"),
    ("023651", "גז טבעי",                "סחורות"),
    ("098662", "מדד הדולר",              "מט\"ח"),
    ("099741", "אירו",                   "מט\"ח"),
    ("097741", "ין יפני",                "מט\"ח"),
    ("096742", "לירה שטרלינג",           "מט\"ח"),
    ("133741", "ביטקוין",                "קריפטו"),
    ("146021", "את'ריום",                "קריפטו"),
]

# Number of trailing weekly COT reports shown as explicit value+change columns
# (the user wants each week of the last month, not a monthly aggregate).
COT_WEEK_COLUMNS = 5

# --- Mid-cap accumulation screen -------------------------------------------
# Companies (not funds) whose market cap sits in this band and that the tracked
# institutions have accumulated across the whole window are surfaced separately.
MIDCAP_MIN_USD = 5_000_000_000
MIDCAP_MAX_USD = 50_000_000_000
# Show only the strongest accumulators (ranked by one-year holding growth).
MIDCAP_LIMIT = 15

# A name qualifies as "accumulated for a year+" when aggregate shares held by
# the tracked institutions rose across every quarter of the window (small
# per-quarter dips are tolerated) and grew at least this much end to end.
ACCUM_MIN_GROWTH = 1.10          # +10% over the year
ACCUM_TOLERANCE = 0.97           # each quarter >= 97% of the prior (allow noise)
# Ignore sub-dollar tickers up front: real mid-caps are not penny stocks, and
# this keeps the enrichment call volume bounded.
ACCUM_PRICE_FLOOR = 4.0

# OpenFIGI securityType2 values counted as an operating "company" (vs a fund).
COMPANY_SECURITY_TYPES = {
    "Common Stock", "ADR", "GDR", "REIT", "Depositary Receipt",
    "Royalty Trust", "Ltd Partnership", "MLP", "Unit",
}

# --- Market-index instruments ----------------------------------------------
# Index ETFs / index funds are pulled out of the ordinary holdings into their
# own section. Detection is by CUSIP first (exact) then issuer-name keywords.
# Only CUSIPs verified against the live 13F data are listed here — generic
# filer names like "ISHARES TR" hide the underlying index, so guessing CUSIPs
# risks tagging the wrong fund (a Treasury or sector ETF) as an index tracker.
# Everything else is caught by INDEX_NAME_PATTERNS below.
INDEX_FUND_CUSIPS = {
    "78462F103": "SPDR S&P 500 ETF (SPY)",
    "464287200": "iShares Core S&P 500 (IVV)",
    "922908363": "Vanguard S&P 500 (VOO)",
    "922908769": "Vanguard Total Stock Market (VTI)",
    "46090E103": "Invesco QQQ (Nasdaq-100)",
    "46138G649": "Invesco NASDAQ 100 (QQQM)",
}

# --- AI value chain -----------------------------------------------------------
# The eight layers of the AI stack, from power generation up to servers. CUSIPs
# were verified against the live holdings (resolved via ticker, not guessed) so
# each maps to the security the institutions actually report. `buying` marks the
# layers the tracked institutions have been accumulating. Up to five names per
# layer; build_ai keeps those actually held, ranked by held value.
# Each layer lists a broad, verified universe of the companies that belong to
# it; build_ai keeps the five the institutions actually hold the most of. CUSIPs
# were confirmed against the live 13F data (name-variant mismatches dropped), so
# the selection is data-driven — largest holding wins — within a fixed taxonomy.
AI_LAYERS = [
    {"n": 1, "he": "ייצור חשמל ואנרגיה", "buying": False, "companies": [
        ("36828A101", "GE Vernova", "GEV"),
        ("65339F101", "NextEra Energy", "NEE"),
        ("842587107", "Southern Company", "SO"),
        ("26441C204", "Duke Energy", "DUK"),
        ("21037T109", "Constellation Energy", "CEG"),
        ("29364G103", "Entergy", "ETR"),
        ("25746U109", "Dominion Energy", "D"),
        ("30161N101", "Exelon", "EXC"),
        ("92840M102", "Vistra", "VST"),
        ("629377508", "NRG Energy", "NRG"),
        ("337932107", "FirstEnergy", "FE"),
        ("69351T106", "PPL", "PPL"),
        ("87422Q109", "Talen Energy", "TLN"),
    ]},
    {"n": 2, "he": "תשתית מרכזי נתונים", "buying": False, "companies": [
        ("G29183103", "Eaton", "ETN"),
        ("92537N108", "Vertiv", "VRT"),
        ("G51502105", "Johnson Controls", "JCI"),
        ("031100100", "Ametek", "AME"),
        ("443510607", "Hubbell", "HUBB"),
        ("G6700G107", "nVent Electric", "NVT"),
    ]},
    {"n": 3, "he": "חומרי גלם ורכיבים", "buying": False, "companies": [
        ("219350105", "Corning", "GLW"),
        ("G54950103", "Linde", "LIN"),
        ("009158106", "Air Products", "APD"),
        ("29362U104", "Entegris", "ENTG"),
        ("553368101", "MP Materials", "MP"),
    ]},
    {"n": 4, "he": "ציוד לייצור שבבים", "buying": True, "companies": [
        ("512807306", "Lam Research", "LRCX"),
        ("038222105", "Applied Materials", "AMAT"),
        ("482480100", "KLA", "KLAC"),
        ("880770102", "Teradyne", "TER"),
        ("N07059210", "ASML", "ASML"),
        ("683344105", "Onto Innovation", "ONTO"),
        ("501242101", "Kulicke & Soffa", "KLIC"),
        ("054540208", "Axcelis", "ACLS"),
    ]},
    {"n": 5, "he": "מפעלי ליהוק (ייצור השבב)", "buying": False, "companies": [
        ("458140100", "Intel", "INTC"),
        ("874039100", "Taiwan Semiconductor", "TSM"),
        ("910873405", "United Microelectronics", "UMC"),
        ("G39387108", "GlobalFoundries", "GFS"),
    ]},
    {"n": 6, "he": "שבבי AI ומעבדים גרפיים", "buying": True, "companies": [
        ("67066G104", "Nvidia", "NVDA"),
        ("11135F101", "Broadcom", "AVGO"),
        ("007903107", "Advanced Micro Devices", "AMD"),
        ("032654105", "Analog Devices", "ADI"),
        ("747525103", "Qualcomm", "QCOM"),
        ("N6596X109", "NXP Semiconductors", "NXPI"),
        ("595017104", "Microchip Technology", "MCHP"),
        ("682189105", "ON Semiconductor", "ON"),
        ("042068205", "Arm Holdings", "ARM"),
    ]},
    {"n": 7, "he": "רשת, זיכרון ואופטיקה", "buying": True, "companies": [
        ("595112103", "Micron Technology", "MU"),
        ("573874104", "Marvell Technology", "MRVL"),
        ("171779309", "Ciena", "CIEN"),
        ("040413205", "Arista Networks", "ANET"),
        ("55024U109", "Lumentum", "LITE"),
        ("19247G107", "Coherent", "COHR"),
        ("04626A103", "Astera Labs", "ALAB"),
        ("G25457105", "Credo Technology", "CRDO"),
    ]},
    {"n": 8, "he": "שרתים, אחסון וחומרה", "buying": False, "companies": [
        ("17275R102", "Cisco Systems", "CSCO"),
        ("958102105", "Western Digital", "WDC"),
        ("G7997R103", "Seagate Technology", "STX"),
        ("24703L202", "Dell Technologies", "DELL"),
        ("42824C109", "Hewlett Packard Enterprise", "HPE"),
        ("64110D104", "NetApp", "NTAP"),
        ("86800U302", "Super Micro Computer", "SMCI"),
    ]},
]


# --- Sector classification (SIC description -> coarse GICS-like sector) ------
# Ordered list; the first substring that appears in the SIC description wins, so
# more specific patterns must come before general ones.
SECTOR_LABELS = {
    "tech": "טכנולוגיה",
    "health": "בריאות",
    "financials": "פיננסים",
    "discretionary": "צריכה מחזורית",
    "staples": "צריכה בסיסית",
    "industrials": "תעשייה",
    "energy": "אנרגיה",
    "realestate": "נדל\"ן",
    "utilities": "תשתיות",
    "materials": "חומרים",
    "communication": "תקשורת",
    "other": "אחר",
}

SECTOR_PATTERNS = [
    # Communication services (before "electronic"/"services")
    ("telephone", "communication"), ("telecommunications", "communication"),
    ("television", "communication"), ("radio broadcast", "communication"),
    ("cable", "communication"), ("motion picture", "communication"),
    ("advertising", "communication"), ("newspaper", "communication"),
    ("publishing", "communication"),
    # Technology
    ("semiconductor", "tech"), ("computer", "tech"), ("software", "tech"),
    ("prepackaged", "tech"), ("programming", "tech"), ("data processing", "tech"),
    ("optical instruments", "tech"), ("electronic", "tech"),
    ("communications equipment", "tech"), ("information retrieval", "tech"),
    # Healthcare
    ("pharmaceutical", "health"), ("biological", "health"), ("medicinal", "health"),
    ("surgical", "health"), ("medical", "health"), ("hospital", "health"),
    ("health", "health"), ("dental", "health"), ("diagnostic", "health"),
    ("in vitro", "health"), ("laborator", "health"),
    # Financials
    ("bank", "financials"), ("security brokers", "financials"),
    ("insurance", "financials"), ("finance", "financials"),
    ("investment", "financials"), ("credit", "financials"),
    ("savings", "financials"), ("fire, marine", "financials"),
    ("title insurance", "financials"),
    # Energy
    ("crude petroleum", "energy"), ("petroleum refining", "energy"),
    ("natural gas", "energy"), ("oil", "energy"), ("coal", "energy"),
    ("drilling", "energy"), ("pipe lines", "energy"),
    # Utilities
    ("electric services", "utilities"), ("gas services", "utilities"),
    ("water supply", "utilities"), ("utilit", "utilities"), ("power", "utilities"),
    # Real estate
    ("real estate", "realestate"), ("reit", "realestate"),
    # Consumer staples
    ("beverages", "staples"), ("soap", "staples"), ("food", "staples"),
    ("grocery", "staples"), ("tobacco", "staples"), ("dairy", "staples"),
    ("retail-variety", "staples"), ("agricultur", "staples"),
    ("bottled", "staples"), ("meat", "staples"),
    # Consumer discretionary
    ("motor vehicle", "discretionary"), ("retail", "discretionary"),
    ("eating places", "discretionary"), ("apparel", "discretionary"),
    ("hotels", "discretionary"), ("footwear", "discretionary"),
    ("household", "discretionary"), ("catalog", "discretionary"),
    ("leisure", "discretionary"), ("furniture", "discretionary"),
    ("services-", "discretionary"),
    # Materials
    ("chemical", "materials"), ("metal", "materials"), ("mining", "materials"),
    ("steel", "materials"), ("gold", "materials"), ("paper", "materials"),
    ("plastics", "materials"), ("industrial inorganic", "materials"),
    ("paints", "materials"),
    # Industrials (broad, near the end)
    ("aircraft", "industrials"), ("aerospace", "industrials"),
    ("machinery", "industrials"), ("construction", "industrials"),
    ("industrial", "industrials"), ("engines", "industrials"),
    ("transportation", "industrials"), ("air transport", "industrials"),
    ("railroad", "industrials"), ("trucking", "industrials"),
    ("engineering", "industrials"), ("manifold", "industrials"),
    ("special industry", "industrials"), ("electrical equipment", "industrials"),
    ("equipment", "industrials"),
]


# Issuer-name keyword patterns that mark a broad-market index tracker. Matched
# case-insensitively against the 13F issuer name as a fallback to the CUSIP set.
INDEX_NAME_PATTERNS = [
    "S&P 500", "S&P500", "SP 500", "NASDAQ-100", "NASDAQ 100", "QQQ",
    "RUSSELL 2000", "RUSSELL 1000", "RUSSELL 3000", "DOW JONES",
    "TOTAL STOCK MARKET", "TOTAL MARKET INDEX", "500 INDEX", "500 ETF",
    "MIDCAP 400", "MID-CAP 400", "SMALLCAP 600", "SMALL-CAP 600",
    "MSCI EAFE", "MSCI ACWI", "MSCI WORLD", "MSCI EMERGING",
]
