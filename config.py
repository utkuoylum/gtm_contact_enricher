import os
from dotenv import load_dotenv

load_dotenv()

# Anthropic Claude API key — used as smart fallback parser when regex extraction fails
# Model: claude-sonnet-4-6 (default), switch to claude-haiku-4-5-20251001 to cut costs ~8x
# Pricing: Sonnet ~$3/1M input + $15/1M output tokens; Haiku ~$0.25/$1.25
# Get key: https://console.anthropic.com/
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
# Fast/cheap model for high-volume structural tasks (domain lookup, scoring, SERP parsing).
# Haiku is ~8x cheaper and ~3x faster than Sonnet for these simple tasks.
CLAUDE_FAST_MODEL = os.getenv("CLAUDE_FAST_MODEL", "claude-haiku-4-5-20251001")

# Hunter.io API key (free: 25 searches/month, paid: $49/mo for 500)
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")

# Optional: ScraperAPI key to bypass Google/LinkedIn blocks ($49/mo for 100k requests)
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

# Optional: Jina AI API key for higher rate limits (free tier works without key)
# Get free key at: https://jina.ai/
JINA_API_KEY = os.getenv("JINA_API_KEY", "")

# Request settings
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT") or "15")
MAX_RETRIES = int(os.getenv("MAX_RETRIES") or "2")
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS") or "1.5")

# SMTP email verification
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT") or "10")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "verify@enrichment.local")

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

# Job agency context: titles that can authorize recruitment agreements
# NOTE: short abbreviations (ceo, coo, cfo, cto, gm, md, vp) are matched as whole
# words in rater.py to avoid false substring matches (e.g. "cto" inside "director").
DECISION_MAKER_TITLES = {
    1: [  # C-suite / Executive / Owner — can sign agency agreements
        # English
        "ceo", "chief executive", "founder", "co-founder", "owner",
        "managing partner", "equity partner", "founding partner", "senior partner",
        "managing director", "md", "president", "chairman", "coo", "chief operating",
        "cfo", "chief financial", "cto", "chief technology", "general manager", "gm",
        "executive director", "director general", "proprietor",
        # German — Geschäftsführer is THE decision-maker in every GmbH/UG
        "geschäftsführer", "geschaeftsfuehrer", "geschaftsfuhrer",
        "geschäftsführerin", "gesellschafter", "gesellschafterin",
        "inhaber", "inhaberin", "vorstand", "vorstandsvorsitzender",
        "vorstandsvorsitzende", "gründer", "gründerin", "mitgründer",
        "geschäftsleiterin", "geschäftsleiter", "betriebsinhaber",
        "alleininhaber", "eigentümer", "eigentümerin",
        # French / Italian (for multinational subsidiaries)
        "directeur général", "directeur", "pdg", "gérant",
        "amministratore delegato", "direttore generale",
    ],
    2: [  # VP / Director level HR & People — can authorize recruitment spend
        # English
        "chief people officer", "cpo", "chief hr officer", "chro",
        "vp hr", "vp of hr", "vp human resources", "vp of human resources",
        "vp people", "vp of people", "vp talent", "vp of talent",
        "director of hr", "director of human resources", "director of people",
        "director of talent", "head of hr", "head of human resources",
        "head of people", "head of talent acquisition", "head of recruiting",
        "head of recruitment", "hr director", "people director", "talent director",
        # German HR/People leadership
        "personalleiter", "personalleiterin", "leiter personal", "leiterin personal",
        "personalchef", "hr-leiter", "hr-leiterin", "hr direktor", "hr direktorin",
        "leiter personalwesen", "leiter human resources",
        "prokurist", "prokuristin",  # Prokurist = authorized signatory, can sign contracts
        "bereichsleiter personal", "abteilungsleiter personal",
        "personalverantwortlicher", "personalverantwortliche",
        "talent acquisition leiter", "recruiting leiter",
    ],
    3: [  # Manager level HR & TA — day-to-day recruitment decisions
        # English
        "hr manager", "human resources manager", "people manager",
        "talent acquisition manager", "recruiting manager", "recruitment manager",
        "talent manager", "staffing manager", "workforce manager",
        "hr business partner", "hrbp", "senior recruiter", "lead recruiter",
        "senior talent", "principal recruiter",
        # German HR Manager level
        "personalmanager", "personalmanagerin", "hr-manager", "hr-managerin",
        "personalreferent", "personalreferentin",
        "recruiter", "recruiterin", "senior recruiter", "lead recruiter",
        "talent acquisition manager", "recruiting manager",
        "personalberater", "personalberaterin",
        "teamleiter personal", "teamleiterin personal",
    ],
    4: [  # Practitioner level — involved in hiring, limited authority
        # English
        "talent acquisition specialist", "talent acquisition consultant",
        "hr specialist", "human resources specialist", "hr generalist",
        "recruitment specialist", "resourcing partner", "people operations",
        "hr advisor", "people advisor", "talent partner",
        # German practitioner level
        "personalsachbearbeiter", "personalsachbearbeiterin",
        "hr-spezialist", "hr-spezialistin", "hr-generalist", "hr-generalistin",
        "personalfachmann", "personalfachfrau",
        "mitarbeiter personal", "mitarbeiterin personal",
        "sachbearbeiter personal",
    ],
    5: [  # Support / other — likely no independent hiring authority
        # English
        "hr coordinator", "hr assistant", "hr administrator", "hr intern",
        "talent coordinator", "recruiting coordinator", "people coordinator",
        "office manager", "administrative", "receptionist",
        # German support level
        "personalkaufmann", "personalkauffrau",
        "hr-koordinator", "hr-koordinatorin",
        "personalassistent", "personalassistentin",
        "hr-assistent", "hr-assistentin",
        "sekretär", "sekretärin", "büroleiter", "büroleiterin",
    ],
}

# German short abbreviations that need word-boundary matching in rater.py
GERMAN_SHORT_ABBREVS = {"gm", "md", "vp", "ceo", "cfo", "cto", "coo", "cpo"}
