STOPWORDS = {
    "the","a","an","and","or","of","to","in","on","for","with","as","at","by","from",
    "today","live","updates","update","says","said","after","before","over","into",
    "india","news","report","reports","will","may","can","how","why","what","when",
    "it","its","they","their","his","her","you","your","is","are","was","were"
}

PUBLISHER_STOPWORDS = {
    "hindu","hindustan","times","toi","ndtv","reuters","bbc","guardian","express",
    "india","today","mint","economic","tribune","telegraph","print","news","live",
    "updates","update","report","reports"
}

SUMMARY_STOPWORDS = {
    "click", "read more", "watch live", "updated", "breaking", "latest", "photos",
    "video", "subscribe", "newsletter"
}

SENSATIONAL_WORDS = {
    "shocking", "explosive", "massive", "huge", "unbelievable", "stunning", "panic",
    "chaos", "bombshell", "exposed", "viral", "dramatic", "outrage", "crisis"
}

BREAKING_KEYWORDS = {
    "breaking", "earthquake", "tsunami", "cyclone", "flood", "war", "attack",
    "explosion", "crash", "wildfire", "emergency", "evacuation", "storm"
}

COUNTRY_OPTIONS = [
    ("World", "WORLD"),
    ("India", "IN"),
    ("United States", "US"),
    ("United Kingdom", "GB"),
    ("Canada", "CA"),
    ("Australia", "AU"),
    ("UAE", "AE"),
    ("Singapore", "SG"),
    ("Japan", "JP"),
    ("Germany", "DE"),
    ("France", "FR"),
]

COUNTRY_CODE_TO_NAME = {code: label for label, code in COUNTRY_OPTIONS}

SOURCE_OPTIONS = [
    ("All Trusted (Default)", ""),
    ("The Hindu", "thehindu.com"),
    ("Hindustan Times", "hindustantimes.com"),
    ("Times of India", "timesofindia.indiatimes.com"),
    ("Economic Times", "economictimes.indiatimes.com"),
    ("Indian Express", "indianexpress.com"),
    ("India Today", "indiatoday.in"),
    ("NDTV", "ndtv.com"),
    ("Livemint", "livemint.com"),
    ("Business Standard", "business-standard.com"),
    ("Moneycontrol", "moneycontrol.com"),
    ("Deccan Herald", "deccanherald.com"),
    ("Reuters", "reuters.com"),
    ("BBC", "bbc.com"),
    ("Associated Press", "apnews.com"),
    ("Bloomberg", "bloomberg.com"),
    ("The Guardian", "theguardian.com"),
    ("TechCrunch", "techcrunch.com"),
    ("The Verge", "theverge.com"),
    ("Ars Technica", "arstechnica.com"),
    ("ESPN", "espn.com"),
    ("Cricbuzz", "cricbuzz.com"),
    ("ESPNcricinfo", "espncricinfo.com"),
    ("Sky Sports", "skysports.com"),
    ("ICC Cricket", "icc-cricket.com"),
    ("BCCI", "bcci.tv"),
    ("IPL", "iplt20.com"),
    ("FIFA", "fifa.com"),
    ("UEFA", "uefa.com"),
    ("Olympics", "olympics.com"),
    ("NBA", "nba.com"),
    ("NFL", "nfl.com"),
    ("MLB", "mlb.com"),
    ("NHL", "nhl.com"),
    ("Formula 1", "formula1.com"),
    ("MotoGP", "motogp.com"),
    ("ATP Tour", "atptour.com"),
    ("WTA Tennis", "wtatennis.com"),
    ("Premier League", "premierleague.com"),
]

SOURCE_SHOWCASE = {
    "Technology": [
        {"name": "Techcrunch", "logo": "/static/images/TC.jpg", "badge": "TC"},
        {"name": "The Verge", "logo": "/static/images/the-verge.jpg", "badge": "TV"},
        {"name": "Wired", "logo": "/static/images/wired.jpg", "badge": "WI"},
        {"name": "Ars Technica", "logo": "/static/images/arstechnica.jpg", "badge": "AT"},
        {"name": "Engadget", "logo": "/static/images/Engadget_Logo.png", "badge": "EN"},
        {"name": "ZDNET", "logo": "/static/images/zdnet.jpg", "badge": "ZD"},
        {"name": "Android Police", "logo": "/static/images/android police.jpg", "badge": "AP"},
        {"name": "Mashable India", "logo": "/static/images/mashable", "badge": "MI"},

    ],
    "Business": [
        {"name": "Reuters", "logo": "/static/images/reuters.jpg", "badge": "RE"},
        {"name": "Bloomberg", "logo": "/static/images/bloomberg.jpg", "badge": "BL"},
        {"name": "CNBC", "logo": "/static/images/Cnbc.jpg", "badge": "CN"},
        {"name": "Financial Times", "logo": "/static/images/financial-times.jpg", "badge": "FT"},
        {"name": "Forbes", "logo": "/static/images/forbes.jpg", "badge": "FO"},
        {"name": "WSJ", "logo": "/static/images/wsj-logo.jpg", "badge": "WS"},
        {"name": "Economic Times", "logo": "/static/images/economic times.jpg", "badge": "ET"},
        {"name": "NDTV Profit", "logo": "/static/images/ndtv-profit.jpg", "badge": "NP"},
        {"name": "Business Standards", "logo": "/static/images/business standards.jpg", "badge": "BS"},
        {"name": "Investment Guru", "logo": "/static/images/investment guru.jpg", "badge": "IG"},
    ],
    "World": [
        {"name": "BBC News", "logo": "/static/images/BBC.jpg", "badge": "BBC"},
        {"name": "NDTV News", "logo": "/static/images/ndtv.jpg", "badge": "ND"},
        {"name": "AL jazeera", "logo": "/static/images/aljazeera.jpg", "badge": "AJ"},
        {"name": "The Guardian", "logo": "/static/images/The-Guardian.jpg", "badge": "GU"},
        {"name": "CNN", "logo": "/static/images/cnn.jpg", "badge": "CNN"},
        {"name": "AP News", "logo": "/static/images/AP.jpg", "badge": "AP"},
    ],
    "India": [
        {"name": "The Hindu", "logo": "/static/images/the hindu.jpg", "badge": "TH"},
        {"name": "Hindustan Times", "logo": "/static/images/hindustan times.jpg", "badge": "HT"},
        {"name": "Indian Express", "logo": "/static/images/the indian express.jpg", "badge": "IE"},
        {"name": "Times of India", "logo": "/static/images/the times of india.jpg", "badge": "TOI"},
        {"name": "India Today", "logo": "/static/images/india today.jpg", "badge": "IT"},
        {"name": "Money Control", "logo": "/static/images/money control.jpg", "badge": "MC"},
        {"name": "Deccan Herald", "logo": "/static/images/deccan herland.jpg", "badge": "DH"},
        {"name": "News18", "logo": "/static/images/news18.jpg", "badge": "N18"},
        {"name": "WION", "logo": "/static/images/wion.jpg", "badge": "WION"},
    ],
    "Sports": [
        {"name": "NDTV sports", "logo": "/static/images/ndtvsports.jpg", "badge": "NS"},
        {"name": "Sport Star", "logo": "/static/images/sportstar.jpg", "badge": "SS"},
        {"name": "IPL T20", "logo": "/static/images/ipl.jpg", "badge": "IPL"},
        {"name": "Chess", "logo": "/static/images/chess.jpg", "badge": "Chess"},
        {"name": "ICC", "logo": "/static/images/icc.jpg", "badge": "ICC"},
        {"name": "Cricketworld", "logo": "/static/images/cricketworld.jpg", "badge": "CW"},
        {"name": "ESPN", "logo": "/static/images/espn.jpg", "badge": "ES"},
        {"name": "Sky Sports", "logo": "/static/images/skysports.jpg", "badge": "SS"},
        {"name": "Cricbuzz", "logo": "/static/images/cricbuzz.jpg", "badge": "CB"},
        {"name": "Sports illustrated", "logo": "/static/images/sportsillustrated.jpg", "badge": "SI"},
        {"name": "Barca Universal", "logo": "/static/images/barcauniversal.jpg", "badge": "BU"},
    ],
    "Entertainment": [
        {"name": "Variety", "logo": "/static/images/variety.jpg", "badge": "VA"},
        {"name": "Hollywood reporter", "logo": "/static/images/hollywood.jpg", "badge": "HR"},
        {"name": "Billboard", "logo": "/static/images/billboard.jpg", "badge": "BB"},
        {"name": "Rolling Stone", "logo": "/static/images/Rolling_Stone.jpg", "badge": "RS"},
        {"name": "123Telugu", "logo": "/static/images/123telugu.jpg", "badge": "123"},
        {"name": "Gulte", "logo": "/static/images/gulte.jpg", "badge": "GU"},
        {"name": "Bollywood Hungama", "logo": "/static/images/bollywoodhungama.jpg", "badge": "BH"},
        {"name": "Sacnilk", "logo": "/static/images/sacnilk.jpg", "badge": "SA"},
    ],
    "Science": [
        {"name": "Space", "logo": "/static/images/space.jpg", "badge": "SP"},
        {"name": "NASA", "logo": "/static/images/nasa.jpg", "badge": "NASA"},
        {"name": "Science Daily", "logo": "/static/images/sciencedaily.jpg", "badge": "SD"},
        {"name": "Science News", "logo": "/static/images/sciencenews.jpg", "badge": "SN"},
    ],
}

SOURCE_QUERY_MAP = {
    "techcrunch.com": "Techcrunch",
    "theverge.com": "The Verge",
    "wired.com": "Wired",
    "arstechnica.com": "Ars Technica",
    "engadget.com": "Engadget",
    "zdnet.com": "ZDNET",
    "androidpolice.com": "Android Police",
    "in.mashable.com": "Mashable india",
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "cnbc.com": "CNBC",
    "ft.com": "Financial Times",
    "forbes.com": "Forbes",
    "wsj.com": "Wall Street Journal",
    "m.economictimes.com": "Economic Times",
    "ndtvprofit.com": "NDTV Profit",
    "business-standard.com": "Business Standards",
    "investmentguruindia.com": "Investment Guru",
    "bbc.com": "BBC News",
    "ndtv.com": "NDTV",
    "aljazeera.com": "AL jazeera",
    "theguardian.com": "The Guardian",
    "cnn.com": "CNN",
    "apnews.com": "AP News",
    "thehindu.com": "The Hindu",
    "hindustantimes.com": "Hindustan Times",
    "indianexpress.com": "Indian Express",
    "timesofindia.indiatimes.com": "Times of India",
    "indiatoday.in": "India Today",
    "moneycontrol.com": "Money Control",
    "deccanherald.com": "Deccan Herland",
    "news18.com": "News18",
    "wionews.com": "WION",
    "espn.com": "ESPN",
    "skysports.com": "Sky Sports",
    "cricbuzz.com": "Cricbuzz",
    "si.com": "Sports Illustrated",
    "sports.ndtv.com": "NDTV Sports",
    "sportstat.thehindu.com": "Sports Star",
    "iplt20.com": "IPL T20",
    "chess.com": "Chess",
    "icc-cricket.com": "ICC",
    "cricketworld.com": "Cricket World",
    "barcauniversal.com": "Barca Universal",
    "variety.com": "Variety",
    "hollywoodreporter.com": "Hollywood Reporter",
    "billboard.com": "Billboard",
    "rollingstone.com": "Rolling Stone",
    "123Telugu.com": "123Telugu",
    "gulte.com": "Gulte",
    "bollywoodhungama.com": "Bollywood Hungama",
    "sacnilk.com": "Sacnilk",
    "space.com": "Space",
    "nasa.gov": "NASA",
    "sciencenews.org": "Science News",
    "sciencedaily.com": "Science Daily",
}

SOURCE_ROUTE_DOMAIN_MAP = {
    "bbc news": "bbc.com",
    "bbc": "bbc.com",
    "ndtv news": "ndtv.com",
    "ndtv": "ndtv.com",
    "reuters": "reuters.com",
    "reuters business": "reuters.com",
    "bloomberg": "bloomberg.com",
    "techcrunch": "techcrunch.com",
    "the verge": "theverge.com",
    "wired": "wired.com",
    "ars technica": "arstechnica.com",
    "engadget": "engadget.com",
    "zdnet": "zdnet.com",
    "cnbc": "cnbc.com",
    "financial times": "ft.com",
    "forbes": "forbes.com",
    "wsj": "wsj.com",
    "wall street journal": "wsj.com",
    "al jazeera": "aljazeera.com",
    "guardian": "theguardian.com",
    "cnn": "cnn.com",
    "ap news": "apnews.com",
    "ap": "apnews.com",
    "the hindu": "thehindu.com",
    "hindustan times": "hindustantimes.com",
    "indian express": "indianexpress.com",
    "times of india": "timesofindia.indiatimes.com",
    "india today": "indiatoday.in",
    "moneycontrol": "moneycontrol.com",
    "espn": "espn.com",
    "sky sports": "skysports.com",
    "cricbuzz": "cricbuzz.com",
    "sports illustrated": "si.com",
    "android police": "androidpolice.com",
    "mashable india": "in.mashable.com",
    "economic times": "m.economictimes.com",
    "ndtv profit": "ndtvprofit.com",
    "business standards": "business-standard.com",
    "business standard": "business-standard.com",
    "investment guru": "investmentguruindia.com",
    "the guardian": "theguardian.com",
    "money control": "moneycontrol.com",
    "deccan herald": "deccanherald.com",
    "news18": "news18.com",
    "wion": "wionews.com",
    "ndtv sports": "sports.ndtv.com",
    "sport star": "sportstar.thehindu.com",
    "sports star": "sportstar.thehindu.com",
    "ipl t20": "iplt20.com",
    "chess": "chess.com",
    "icc": "icc-cricket.com",
    "cricketworld": "cricketworld.com",
    "cricket world": "cricketworld.com",
    "barca universal": "barcauniversal.com",
    "variety": "variety.com",
    "hollywood reporter": "hollywoodreporter.com",
    "billboard": "billboard.com",
    "rolling stone": "rollingstone.com",
    "123telugu": "123telugu.com",
    "gulte": "gulte.com",
    "bollywood hungama": "bollywoodhungama.com",
    "sacnilk": "sacnilk.com",
    "space": "space.com",
    "nasa": "nasa.gov",
    "science daily": "sciencedaily.com",
    "science news": "sciencenews.org",
}

SOURCE_FEED_MAP = {
    "techcrunch.com": [
        "https://techcrunch.com/feed/"
    ],
    "theverge.com": [
        "https://www.theverge.com/rss/index.xml"
    ],
    "arstechnica.com": [
        "https://feeds.arstechnica.com/arstechnica/index"
    ],
    "bbc.com": [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/technology/rss.xml"
    ],
    "ndtv.com": [
        "https://feeds.feedburner.com/ndtvnews-top-stories",
        "https://feeds.feedburner.com/ndtvnews-india-news",
        "https://feeds.feedburner.com/ndtvnews-world-news"
    ],
    "reuters.com": [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.reuters.com/reuters/technologyNews"
    ],
    "wired.com": [
        "https://www.wired.com/feed/rss"
    ],
    "zdnet.com": [
        "https://www.zdnet.com/news/rss.xml"
    ],
    "engadget.com": [
        "https://www.engadget.com/rss.xml"
    ],
    "cnn.com": [
        "http://rss.cnn.com/rss/edition.rss"
    ],
    "theguardian.com": [
        "https://www.theguardian.com/world/rss",
        "https://www.theguardian.com/technology/rss"
    ],
    "aljazeera.com": [
        "https://www.aljazeera.com/xml/rss/all.xml"
    ],
    "apnews.com": [
        "https://apnews.com/hub/ap-top-news?output=amp"
    ],
    "cnbc.com": [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html"
    ],
    "espn.com": [
        "https://www.espn.com/espn/rss/news"
    ],
    "variety.com": [
        "https://variety.com/feed/"
    ],
    "androidpolice.com": [
        "https://www.androidpolice.com/feed/"
    ],
    "in.mashable.com": [
        "https://in.mashable.com/feeds/rss/all"
    ],
    "m.economictimes.com": [
        "https://m.economictimes.com/rssfeedsdefault.cms"
    ],
    "business-standard.com": [
        "https://www.business-standard.com/rss/home_page_top_stories.rss"
    ],
    "news18.com": [
        "https://www.news18.com/rss/india.xml"
    ],
    "wionews.com": [
        "https://www.wionews.com/rss/world.xml"
    ],
    "sports.ndtv.com": [
        "https://feeds.feedburner.com/ndtvsports-latest"
    ],
    "cricketworld.com": [
        "https://www.cricketworld.com/rss.xml"
    ],
    "space.com": [
        "https://www.space.com/feeds/all"
    ],
    "nasa.gov": [
        "https://www.nasa.gov/news-release/feed/"
    ],
    "sciencedaily.com": [
        "https://www.sciencedaily.com/rss/all.xml"
    ],
    "sciencenews.org": [
        "https://www.sciencenews.org/feed"
    ]
}

SOURCE_DOMAIN_ALIASES = {
    "in.mashable.com": ["mashable.com"],
    "zdnet.com": ["www.zdnet.com"],
    "bbc.com": ["bbc.co.uk", "www.bbc.com"],
    "ndtv.com": ["www.ndtv.com"],
    "ndtvprofit.com": ["www.ndtvprofit.com"],
    "apnews.com": ["apnews.com", "www.apnews.com"],
    "theguardian.com": ["www.theguardian.com"],
    "techcrunch.com": ["www.techcrunch.com"],
    "theverge.com": ["www.theverge.com"],
}

SOURCE_FETCH_VARIANTS = {
    "technology": ["technology", "ai", "gadgets", "software"],
    "business": ["business", "markets", "economy", "finance"],
    "world": ["world news", "international news"],
    "india": ["india news", "india politics", "india business"],
    "sports": ["sports", "cricket", "football", "tennis"],
    "entertainment": ["entertainment", "movies", "music", "celebrity"],
    "climate": [
        "climate change",
        "environment",
        "global warming",
        "renewable energy",
        "extreme weather",
        "sustainability"
    ],
}

TRUSTED_SHOWCASE_QUERY_MAP = {
    "Technology": {"category": "technology"},
    "Business": {"category": "business"},
    "World": {"query": "world news"},
    "India": {"query": "india news"},
    "Sports": {"category": "sports"},
    "Entertainment": {"category": "entertainment"},
}

COUNTRY_NAME_TO_CODE = {
    "world": "WORLD",
    "india": "IN",
    "united states": "US",
    "usa": "US",
    "us": "US",
    "america": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "britain": "GB",
    "england": "GB",
    "canada": "CA",
    "australia": "AU",
    "uae": "AE",
    "united arab emirates": "AE",
    "singapore": "SG",
    "japan": "JP",
    "germany": "DE",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "russia": "RU",
    "qatar": "QA",
    "saudi arabia": "SA",
    "china": "CN",
    "brazil": "BR",
    "south africa": "ZA",
    "pakistan": "PK",
    "bangladesh": "BD",
    "sri lanka": "LK",
    "nepal": "NP",
}

CATEGORY_QUERY = {
    "technology": "technology",
    "business": "business",
    "health": "health",
    "sports": "sports OR cricket OR football OR tennis OR formula 1 OR olympics OR fifa OR uefa OR nba OR premier league",
    "politics": "politics",
    "entertainment": "entertainment OR celebrity OR movie OR movies OR film OR cinema OR music OR streaming OR netflix OR bollywood OR hollywood OR tollywood OR web series OR OTT",
    "disaster": "accident OR disaster OR earthquake OR tsunami OR flood OR cyclone OR fire OR explosion OR landslide",
    "climate": "climate OR climate change OR environment OR global warming OR pollution OR carbon emissions OR renewable energy OR sustainability OR conservation OR extreme weather OR wildlife OR biodiversity"
}

TRUSTED_ONLY_CATEGORIES = {"disaster"}
