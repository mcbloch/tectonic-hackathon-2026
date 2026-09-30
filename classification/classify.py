#!/usr/bin/env python3
"""SD Worx knowledge classifier.

Takes one piece of organisational knowledge and produces a schema-v2 classification.

    ingest -> deterministic rules (prior) -> agent (LLM, optional)
           -> client-master inheritance -> validation -> emit

Design rule: exact keys (codes, dates, form names) come from regex, judgment calls
(doc_type, process_domain, worker category) come from the agent, and the validator is
the only writer of the final record. Nothing the agent says reaches the index unchecked.

Backends:
    --backend offline   rules only (no network, always works, this is the demo fallback)
    --backend openai    any OpenAI-compatible /chat/completions endpoint (see .env.example)

Usage:
    python3 classify.py --text "Notulen OR: nieuwe ploegenpremie vanaf 1 maart"
    python3 classify.py --file inbox/klantinstructie.txt
    python3 classify.py --stdin < paste.txt
    python3 classify.py --demo 20                 # first 20 seed docs from schema/documents.json
    python3 classify.py --demo 100 --compare schema/tagged.v2.projected.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "schema"
OUT_DIR = ROOT / "out"

SCHEMA = json.loads((SCHEMA_DIR / "schema.v2.json").read_text())
ENUMS = {k: v["v"] for k, v in SCHEMA["fields"].items() if isinstance(v, dict) and "v" in v}
CLIENTS = json.loads((ROOT / "clients.json").read_text())["clients"] if (ROOT / "clients.json").exists() else {}
PROMPT = (ROOT / "prompt.md").read_text() if (ROOT / "prompt.md").exists() else "Classify the document. Return JSON."

MULTI = {"process_domain", "worker_category", "pay_component", "service_line", "rule_level",
         "sub_jurisdiction", "output_channel", "country"}
TRISTATE = {"*", "n/a"}          # allowed beside null for every field
PROTECTED = {"lang", "legal_basis", "temporal_type", "valid_from", "valid_to", "last_reviewed"}
INHERITED = {"client_id", "client_scope", "pc_code", "sector_agreement", "country", "coverage"}

LEVEL_ORDER = ["eu", "federal", "regional", "sector", "company", "works_council", "client", "individual"]
SCHEME_LEVEL = {"eu_reg": "eu", "law": "federal", "kb": "federal", "case": "federal", "form": "federal",
                "cao": "sector", "be_pc": "sector", "fr_idcc": "sector", "de_tarif": "sector",
                "it_ccnl": "sector", "es_convenio": "sector", "nl_cao": "sector", "ch_gav": "sector",
                "at_kv": "sector", "lu_cct": "sector", "se_kollektivavtal": "sector", "no_tariffavtale": "sector"}
SCHEME_COUNTRY = {"be_pc": "BE", "cao": "BE", "fr_idcc": "FR", "de_tarif": "DE", "it_ccnl": "IT",
                  "es_convenio": "ES", "nl_cao": "NL", "ch_gav": "CH", "at_kv": "AT", "lu_cct": "LU",
                  "se_kollektivavtal": "SE", "no_tariffavtale": "NO", "eu_reg": "EU"}

# ---------------------------------------------------------------- ingestion

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv", ".log", ".eml", ".srt", ".vtt"}


def ingest(raw: str) -> str:
    """Normalise whatever we were handed into plain text."""
    if raw.lstrip()[:1] == "<":
        raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        raw = (raw.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                  .replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"[ \t]+", " ", raw).strip()


def read_source(path: Path) -> str:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise SystemExit(f"{path}: binary format not supported yet (add an extractor). "
                         f"Supported: {', '.join(sorted(TEXT_SUFFIXES))}")
    return ingest(path.read_text(encoding="utf-8", errors="replace"))


# ---------------------------------------------------------------- rules (prior)

LANG_WORDS = {
    "nl": set("de het een van voor met wordt niet op aan bij is dat zijn en je wij ook als dan om te".split()),
    "fr": set("de la le les des pour avec est ne pas sur une du dans que qui sont au aux ce cette".split()),
    "en": set("the of and for with is not on to a in be this that are from will can should".split()),
    "de": set("und der die das mit für ist nicht auf den von werden im dem ein eine auch bei".split()),
}

DOC_TYPE_RULES = [
    ("client_instruction", r"klantinstructie|client instruction|client_instruction|klantspecifiek"),
    ("legal_summary",      r"sectorfiche|wetswijziging|wetgeving|eenheidsstatuut|interpretatienota|adviesnota|"
                           r"arrest|wet van|koninklijk besluit|\bcao \d|loonwet|arbeidswet"),
    ("system",             r"interface|api|integratie|koppeling|release notes|release- en change|validatieregels|"
                           r"specificatie|migratie|import|datakwaliteit"),
    ("control",            r"checklist|vierogen|steekproef|kwaliteitscontrole|testpakket|controle"),
    ("template",           r"sjabloon|template|model |formulier|dpa-sjabloon"),
    ("procedure",          r"procedure|draaiboek|werkinstructie|stappenplan|handleiding|richtlijn|noodplan|"
                           r"implementatiechecklist|onboarding|proefrun"),
    ("comms",              r"e-mail|notulen|ticketoplossing|intern bericht|mail |klacht"),
    ("reference",          r"overzicht|handboek|gids|\bfiche\b|faq|kalender|matrix|kpi|opleidingsgids|roadmap|"
                           r"praktijkgids|monitor"),
]

PROCESS_LEXICON = [
    ("declarations",     r"dmfa|dimona|belcotax|aangifte|declar|dsn|uniemens|\brti\b|\bfps\b|deuev|loonaangifte|"
                         r"a-melding|siltra|elda|ccss|a1|limosa|sociale balans|verplichting"),
    ("absence",          r"vakantie|verlof|absenc|ziekte|ziekenfonds|arbeidsongeschikt|afwezigheid|sick|leave|"
                         r"feestdag|tijdskrediet|moederschap|ouderschap|arbeidsongeval"),
    ("benefits",         r"maaltijdcheque|ecocheque|bedrijfswagen|cafetaria|mobiliteitsbudget|groepsverzekering|"
                         r"pensioen|hospitalisatie|voordeel alle aard|bonusplan|winstpremie|in-kind|extralegaal"),
    ("termination",      r"ontslag|uitdienst|opzegging|severance|outplacement|einde arbeidsovereenkomst|"
                         r"collectief ontslag|swt|landingsbaan|eindeloopbaan"),
    ("deductions",       r"inhouding|loonbeslag|derdenbeslag|overdracht|bedrijfsvoorheffing|précompte|rzv|"
                         r"solidariteitsbijdrage|bijdrage|premie"),
    ("payment",          r"betalingsbestand|sepa|betaling|betalen|payment|bankbestand|loonfiche|aflever"),
    ("systems",          r"interface|\bapi\b|integratie|koppeling|loonmotor|import|systeem|platform|mySDWorx"),
    ("time_attendance",  r"tijdregistratie|klok|overuren|ploegen|shift|uurregistratie|rooster"),
    ("year_end",         r"jaarafsluiting|jaareinde|eindejaar|jaarlijks|sociale balans|new-year|jaarrekening"),
    ("implementation",   r"onboarding|implementatie|inrichting|parameter|stamgegevens|proefrun|migratie"),
    ("input",            r"input|datalevering|aanlevering|mutatie|master data|import"),
    ("quality",          r"kwaliteit|steekproef|audit|controle|vierogen|fout|incident|ticketoplossing"),
    ("compliance",       r"gdpr|privacy|verwerkingsregister|bewaartermijn|dpa|dataprotectie"),
    ("immigration",      r"grensarbeider|expat|inpat|detachering|immigratie|tewerkstelling in het buitenland|a1"),
    ("governance",       r"sla|kpi|escalatie|klacht|dienstenmatrix|governance|verantwoordelijk|opleiding|training"),
    ("reporting",        r"rapportage|reporting|boekhouding|aansluiting|gl-export|kostenrapport"),
]

PAY_LEXICON = [
    ("dbl_holiday",     r"dubbel vakantiegeld|double holiday|vakantiegeld|prime de vacances"),
    ("holiday_pay",     r"vakantie-uitkering|vakantiewetgeving|bouwverlof|jaarlijkse vakantie"),
    ("eoy_bonus",       r"eindejaarspremie|13e maand|13de maand|quattordicesima|tredicesima"),
    ("overtime",        r"overuur|overwerk|overtime|heures suppl"),
    ("shift",           r"ploegenpremie|shiftpremie|nachtpremie|nachtschicht|shift|ploeg"),
    ("severance",       r"opzeggingsvergoeding|severance|verbrekingsvergoeding|outplacement|eindvergoeding"),
    ("index",           r"indexering|spilindex|loonschaal|barema|indexation|scale"),
    ("meal_voucher",    r"maaltijdcheque|titre-restaurant|ticket restaurant"),
    ("eco_voucher",     r"ecocheque|éco-chèque"),
    ("car",             r"bedrijfswagen|company car|firmawagen"),
    ("vaa",             r"voordeel alle aard|avantage de toute nature|benefit in kind|sachbezug"),
    ("mobility",        r"mobiliteitsbudget|mobility budget"),
    ("cafeteria",       r"cafetariaplan|cafeteria"),
    ("group_ins",       r"groepsverzekering|hospitalisatie|group insurance"),
    ("hosp",            r"hospitalisatieverzekering"),
    ("pension",         r"aanvullend pensioen|pensioen|pension|lpp|bvg|ppk|auto-enrolment"),
    ("wage_garnish",    r"loonbeslag|derdenbeslag|cessie|garnishment|saisie"),
    ("sick_pay",        r"gewaarborgd loon|ziekte-uitkering|sick pay|krankengeld|incapacidad temporal"),
    ("telework",        r"telewerk|thuiswerk|home office|télétravail|telework"),
    ("bonus",           r"bonusplan|winstpremie|cao 90|non-recurring"),
]

WORKER_LEXICON = [
    ("arbeider",       r"arbeider|ouvrier|blue[- ]collar"),
    ("bediende",       r"bediende|employé|white[- ]collar|angestellte"),
    ("kader",          r"\bkader\b|cadre|quadro|leitende"),
    ("executive",      r"executive|directielid|bedrijfsleider"),
    ("statutair",      r"statutair|ambtenaar|fonctionnaire"),
    ("flexi",          r"flexi[- ]?job"),
    ("jobstudent",     r"jobstudent|student|werkstudent|stagiar"),
    ("uitzendkracht",  r"uitzendkracht|interim|agency worker|intérim|interinale|zeitarbeit"),
    ("inpat",          r"inpat|expat|detachering|posted worker|30%-regeling"),
    ("frontier",       r"grensarbeider|frontier|frontalier"),
    ("public",         r"publieke sector|overheid|civil servant|openbaar"),
]

COUNTRY_LEXICON = [
    ("BE", r"\bbelgi\w*|\bbelgique\b|\bbelgium\b|\bantwerpen\b|\bbrussel\b|\bbruxelles\b|\bgent\b|\bgand\b|\bleuven\b|\bvlaanderen\b|\bwalloni[eë]\b"),
    ("NL", r"\bnederland\w*|\bnetherlands\b|\butrecht\b|\bamsterdam\b|\brotterdam\b"),
    ("FR", r"\bfrance\b|\bfrankrijk\b|\bparis\b|\blyon\b|\bmarseille\b"),
    ("DE", r"\bdeutschland\b|\bgermany\b|\bduitsland\b|\bberlin\b|\bm[uü]nchen\b|\bmunich\b|\bbundesland\b"),
    ("IT", r"\bitali[aë]\b|\bitaly\b|\bmilano\b|\bmilan\b|\broma\b"),
    ("ES", r"\bespa[nñ]a\b|\bspain\b|\bspanje\b|\bmadrid\b|\bbarcelona\b"),
    ("GB", r"\bunited kingdom\b|\buk\b|\bengland\b|\bscotland\b|\blondon\b"),
    ("IE", r"\bireland\b|\bierland\b|\bdublin\b"),
    ("LU", r"\bluxembourg\b|\bluxemburg\b"),
    ("AT", r"\b[oö]sterreich\b|\baustria\b|\boostenrijk\b|\bwien\b|\bvienna\b"),
    ("CH", r"\bschweiz\b|\bswitzerland\b|\bzwitserland\b|\bz[uü]rich\b|\bzurich\b"),
    ("PL", r"\bpolska\b|\bpoland\b|\bpolen\b|\bkatowice\b|\bwarszawa\b"),
    ("MU", r"\bmauritius\b|\bport louis\b"),
    ("RO", r"\bromania\b|\broemeni[eë]\b|\bbucharest\b|\bboekarest\b"),
    ("AL", r"\balbani[aë]\b|\btirana\b"),
    ("HR", r"\bcroatia\b|\bkroati[eë]\b|\bzagreb\b"),
    ("SE", r"\bsweden\b|\bzweden\b|\bstockholm\b"),
    ("NO", r"\bnorway\b|\bnoorwegen\b|\boslo\b"),
    ("FI", r"\bfinland\b|\bhelsinki\b"),
    ("DK", r"\bdenmark\b|\bdenemarken\b|\bcopenhagen\b|\bkopenhagen\b"),
]

LEGAL_PATTERNS = [
    (r"\bCAO\s*(?:nr\.?|nummer|n°)?\s*(\d{1,3}(?:\.\d{2})?)\b", "cao"),
    (r"\b(?:PC|CP|PC-)\s?(\d{3})\b", "be_pc"),
    (r"paritair\s+comit[eé]\s*(?:nr\.?\s*)?(\d{3})", "be_pc"),
    (r"\bIDCC\s*(\d{4})\b", "fr_idcc"),
    (r"convention\s+collective[^\n]{0,80}?(\d{4})", "fr_idcc"),
    (r"(\bCCNL\w*)", "it_ccnl"),
    (r"(\bconvenio\s+colectivo\w*)", "es_convenio"),
    (r"(\bTarifvertrag\w*)", "de_tarif"),
    (r"\b(?:verordening|règlement|regulation)\s*\(?(?:EU|EG|CE)\)?\s*(?:nr\.?\s*)?(\d{2,4}/\d{2,4})", "eu_reg"),
    (r"\b883/2004\b", "eu_reg"),
    (r"\bwet\s+van\s+\d{1,2}\s+\w+\s+\d{4}\b", "law"),
    (r"\bkoninklijk\s+besluit\b|\bKB\s+van\b", "kb"),
]

CHANNEL_LEXICON = [
    ("dmfa", r"\bdmfa\b"), ("dimona", r"\bdimona\b"), ("belcotax", r"belcotax|281\.(?:10|11|50)"),
    ("bedrijfsvoorheffing", r"bedrijfsvoorheffing|précompte professionnel"), ("rsz_quarterly", r"kwartaalaangifte|\brsz\b|\bnssо\b"),
    ("dsn", r"\bdsn\b"), ("uniemens", r"\buniemens\b"), ("cu", r"certificazione unica|\bCU\b"),
    ("rti_fps", r"\brti\b|\bfps\b"), ("p11d", r"\bp11d\b"), ("deuev", r"\bdeuev\b"),
    ("loonaangifte", r"\bloonaangifte\b"), ("upa", r"\bupa\b"), ("a_melding", r"a-melding"),
    ("siltra", r"siltra|sistema red"), ("certifica", r"certific@"+r"|certifica\b"),
    ("agi", r"\bagi\b"), ("elda", r"\belda\b"), ("ccss", r"\bccss\b"),
    ("revenue_payroll_submission", r"payroll submission|\brps\b"),
    ("incomes_register", r"incomes register|inkomstenregister"),
    ("a1", r"\ba1\b"), ("limosa", r"\blimosa\b"), ("nbb", r"sociale balans|nationale bank"), ("co2", r"\bco2\b"),
]

MONTHS = {m: i for i, m in enumerate(
    ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
     "november", "december", "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
     "septembre", "octobre", "novembre", "décembre"], start=1)}
MONTHS.update({"fevrier": 2, "aout": 8, "decembre": 12})

TEMPORAL_RULES = [
    ("release",   r"release notes|release- en change|\br[12]\s*20\d{2}\b"),
    ("one_off",   r"eenmalig|one-off|tijdelijk|ad hoc|éénmalig"),
    ("periodic",  r"kalender|kwartaal|maandelijks|jaarlijks|jaarlijkse|annual|quarterly|roadmap|monitor|editie 20\d\d"),
    ("law_window", r"wet van|koninklijk besluit|cao \d|arrest|wetgeving|regelgeving"),
]


def _hits(text: str, pattern: str) -> list[str]:
    return [m.group(0) for m in re.finditer(pattern, text, re.I)]


def detect_lang(text: str) -> tuple[str, float]:
    tokens = re.findall(r"[a-zà-ÿ]+", text.lower())
    if len(tokens) < 15:
        return "nl", 0.3
    scores = {lang: sum(1 for t in tokens if t in words) / max(1, len(tokens))
              for lang, words in LANG_WORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    conf = min(0.95, 0.5 + (top[1] - second[1]) * 8)
    return (top[0] if top[1] else "nl"), round(conf, 2)


def rules_pass(text: str, meta: dict) -> tuple[dict, dict]:
    """Deterministic prior. Everything here is cheap, reproducible and free."""
    f: dict = {}
    fm: dict = {}

    def put(field, value, conf, evidence):
        f[field] = value
        fm[field] = {"by": "rule", "confidence": conf, "evidence": evidence[:160]}

    low = text.lower()
    name = meta.get("name", "") + " " + meta.get("path", "")
    blob = name.lower() + " " + low

    lang, lconf = detect_lang(text)
    put("lang", lang, lconf, f"detected from token distribution")

    # legal basis + reporting channels
    bases = []
    for pattern, scheme in LEGAL_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            ident = (m.group(1) if m.groups() else m.group(0)).strip()
            # one pattern often matches inside another ("IDCC 1486" vs "convention collective IDCC 1486")
            if any(b["scheme"] == scheme and (ident in b["id"] or b["id"] in ident) for b in bases):
                continue
            bases.append({"scheme": scheme, "id": ident})
    bases = [b for b in bases
             if not any(b is not o and b["scheme"] == o["scheme"] and b["id"] in o["id"] for o in bases)]
    if bases:
        put("legal_basis", bases, 0.8, "; ".join(b["id"] for b in bases))
        f["rule_level"] = sorted({SCHEME_LEVEL[b["scheme"]] for b in bases if b["scheme"] in SCHEME_LEVEL},
                                 key=LEVEL_ORDER.index)
        fm["rule_level"] = {"by": "derived", "confidence": 0.8, "evidence": "from legal_basis.scheme"}
        countries = sorted({SCHEME_COUNTRY[b["scheme"]] for b in bases if b["scheme"] in SCHEME_COUNTRY})
        if countries:
            put("country", countries if len(countries) > 1 else countries[0], 0.7, "inferred from legal_basis scheme")
    channels = sorted({c for c, p in CHANNEL_LEXICON if re.search(p, text, re.I)})
    if channels:
        put("output_channel", channels, 0.8, ", ".join(channels))

    # client
    clients_found = set()
    for m in re.finditer(r"\[([^\]]{2,40})\]", meta.get("name", "")):
        clients_found.add(m.group(1))
    for m in re.finditer(r"clients?/([A-Za-z0-9 _-]{2,40})/", meta.get("path", "")):
        clients_found.add(m.group(1))
    for cid, c in CLIENTS.items():
        needle = c["name"].lower()
        if needle in blob or any(needle in cf.lower() or cf.lower() in needle for cf in clients_found):
            put("client_id", cid, 0.75, f"client name match: {c['name']}")
            break

    # country (explicit mentions beat scheme inference)
    if not f.get("country"):
        found = [iso for iso, p in COUNTRY_LEXICON if re.search(p, low)]
        if found:
            put("country", found if len(found) > 1 else found[0], 0.6, "country mention")
    # external / non-payroll country => this is a partner-covered market
    if f.get("country") and f["country"] not in ("BE",):
        put("coverage", {"coverage_tier": 2, "pack_status": "partial"}, 0.5, "non-BE country mention")

    # doc_type from title/path first, then first 400 chars
    for dtype, pattern in DOC_TYPE_RULES:
        m = re.search(pattern, name, re.I) or re.search(pattern, text[:600], re.I)
        if m:
            put("doc_type", dtype, 0.65 if m.group(0) else 0.5, m.group(0))
            break

    # process domains + pay components + worker categories by lexicon
    pd = [d for d, p in PROCESS_LEXICON if re.search(p, low)]
    if pd:
        put("process_domain", pd, 0.55, ", ".join(pd))
    pc = [c for c, p in PAY_LEXICON if re.search(p, low)]
    if pc:
        put("pay_component", pc, 0.55, ", ".join(pc))
    wc = [w for w, p in WORKER_LEXICON if re.search(p, low)]
    if wc:
        put("worker_category", wc, 0.5, ", ".join(wc))

    # temporal profile
    for ttype, pattern in TEMPORAL_RULES:
        if re.search(pattern, name, re.I) or re.search(pattern, text[:400], re.I):
            put("temporal_type", ttype, 0.6, pattern)
            break
    else:
        put("temporal_type", "evergreen", 0.5, "no temporal marker found")
    # a date only counts as valid_from when it sits next to a temporal cue - a bare date in a
    # document is usually a publication or reference date, and a wrong valid_from is expensive
    CUE = (r"(?:vanaf|van toepassing|in werking|ingangsdat\w+|met ingang van|geldig(?:heid)?\s+(?:vanaf|tot)|"
           r"ab dem|in kraft|à compter du|dès le|applicable (?:dès|à partir)|effective|from)\D{0,40}?")
    date_iso = None
    m = re.search(CUE + r"(20\d{2})-(\d{2})-(\d{2})", text, re.I)
    if m:
        date_iso = "-".join(m.groups())
    else:
        m = re.search(CUE + r"(\d{1,2})[./](\d{1,2})[./](20\d{2})", text, re.I)
        if m:
            d, mth, y = m.groups()
            date_iso = f"{y}-{int(mth):02d}-{int(d):02d}"
        else:
            m = re.search(CUE + r"(\d{1,2})\s+(januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|"
                          r"november|december|janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|"
                          r"novembre|décembre)\s+(20\d{2})", text, re.I)
            if m:
                date_iso = f"{m.group(3)}-{MONTHS.get(m.group(2).lower(), 1):02d}-{int(m.group(1)):02d}"
    if date_iso:
        f["valid_from"] = date_iso
        fm["valid_from"] = {"by": "rule", "confidence": 0.7, "evidence": date_iso}
    else:
        f["valid_from"] = None
        fm["valid_from"] = {"by": "rule", "confidence": 0.4, "evidence": "no validity start found in the text"}
    if f.get("temporal_type") == "evergreen":
        f["last_reviewed"] = meta.get("mtime", datetime.now(timezone.utc).date().isoformat())
        fm["last_reviewed"] = {"by": "derived", "confidence": 0.4, "evidence": "file mtime"}
    else:
        f.setdefault("last_reviewed", None)
        fm.setdefault("last_reviewed", {"by": "derived", "confidence": 0.2, "evidence": "not set"})

    # scoring / visibility / scope
    dtype = f.get("doc_type")
    if dtype in ("procedure", "control", "client_instruction", "legal_summary"):
        bind, bconf, bevid = "binding", 0.55, "internal procedures and rule restatements are binding on staff"
    elif dtype in ("comms", "template"):
        bind, bconf, bevid = "practice", 0.4, "mail / minutes / ticket / template - informational"
    else:
        bind, bconf, bevid = "binding", 0.35, "default for reference material"
    f["bindingness"] = bind
    fm["bindingness"] = {"by": "rule", "confidence": bconf, "evidence": bevid}
    f["visibility"] = "client" if f.get("client_id") else "all"
    fm["visibility"] = {"by": "rule", "confidence": 0.6, "evidence": "client scope"}
    f["client_scope"] = "per_client" if f.get("client_id") else "generic"
    fm["client_scope"] = {"by": "derived", "confidence": 0.7, "evidence": "client_id presence"}
    f["doc_id"] = meta.get("doc_id")
    f["title"] = meta.get("name")
    return f, fm


# ---------------------------------------------------------------- inheritance

def inherit_from_client(fields: dict, fm: dict) -> list[str]:
    """pc_code / country / coverage come from the client master, never from the text."""
    issues = []
    cid = fields.get("client_id")
    if not cid or cid not in CLIENTS:
        return issues
    c = CLIENTS[cid]
    fields["country"] = c.get("countries") or c.get("country")
    fm["country"] = {"by": "inherit", "confidence": 0.95, "evidence": f"client master {cid}"}
    fields["coverage"] = {"coverage_tier": c.get("coverage_tier"), "pack_status": c.get("pack_status")}
    fm["coverage"] = {"by": "inherit", "confidence": 0.95, "evidence": f"client master {cid}"}
    sa = c.get("sector_agreement")
    if sa:
        fields["sector_agreement"] = sa
        fm["sector_agreement"] = {"by": "inherit", "confidence": 0.95,
                                  "evidence": f"client master {cid}" + (" (code not maintained)" if not sa.get("code") else "")}
    if c.get("pc_code"):
        fields["pc_code"] = c["pc_code"]
        fm["pc_code"] = {"by": "inherit", "confidence": 0.95, "evidence": f"client master {cid}"}
    else:
        fields["pc_code"] = None
        fm["pc_code"] = {"by": "inherit", "confidence": 0.95, "evidence": f"client master {cid}: not maintained"}
        code_label = "joint committee (pc_code)" if c.get("country") == "BE" else f"sector agreement code ({sa['scheme'] if sa else 'scheme unknown'})"
        issues.append(f"pc_code: client master has no {code_label} - fill it in clients.json")
    if "coverage" in fields and fields["coverage"].get("pack_status") != "full":
        issues.append(f"coverage: pack_status={fields['coverage'].get('pack_status')} - answer must carry a low-confidence caveat")
    return issues


# ---------------------------------------------------------------- agent

TOOLS_SPEC = [{
    "type": "function",
    "function": {
        "name": "lookup_client",
        "description": "Resolve a client name to its master record (country, joint committee, coverage tier).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
}]


def run_tool(call: dict) -> dict:
    name, args = call["function"]["name"], json.loads(call["function"].get("arguments") or "{}")
    if name != "lookup_client":
        return {"error": f"unknown tool {name}"}
    q = (args.get("name") or "").lower()
    for cid, c in CLIENTS.items():
        if q and (q in c["name"].lower() or c["name"].lower() in q):
            return {"client_id": cid, **c}
    return {"error": "no match", "hint": "ask the user or leave client_id null and add it to needs_review"}


def parse_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def agent(send, meta: dict, text: str, prior: dict, max_steps: int = 3, system: str | None = None) -> dict | None:
    """Tiny ReAct loop: the model may call lookup_client, then must answer with JSON."""
    payload = {
        "source": {k: meta.get(k) for k in ("doc_id", "name", "path")},
        "deterministic_prior": prior,
        "text": text[:12000],
    }
    messages = [{"role": "system", "content": system or PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    for _ in range(max_steps):
        reply = send(messages)
        calls = reply.get("tool_calls") or []
        if not calls:
            return parse_json(reply.get("content") or "")
        messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(run_tool(call), ensure_ascii=False)})
    return None


TODAY = datetime.now(timezone.utc).date().isoformat()


def MTIME(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def load_env() -> None:
    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def make_openai_send(model: str, use_tools: bool):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY not set - use --backend offline or fill classification/.env")

    def send(messages):
        body = {"model": model, "messages": messages, "temperature": 0}
        if use_tools:
            body["tools"] = TOOLS_SPEC
        else:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{base}/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=int(os.environ.get("CLASSIFY_TIMEOUT", 90))) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"LLM call failed: {e.code} {e.read()[:300]!r}")
        except urllib.error.URLError as e:
            raise SystemExit(f"LLM unreachable: {e.reason}")
        msg = data["choices"][0]["message"]
        return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls")}

    return send


# ---------------------------------------------------------------- validation

def check_value(field: str, value):
    """Return (normalised, [issues]). The validator is the only writer of record fields."""
    issues = []
    if value is None or value == "*" or value == "n/a":
        return value, issues
    allowed = ENUMS.get(field)
    if field == "legal_basis":
        if isinstance(value, dict):
            value = [value]
        out = []
        for b in value if isinstance(value, list) else []:
            if not isinstance(b, dict) or "scheme" not in b:
                issues.append(f"legal_basis: malformed entry {b!r}")
                continue
            scheme = b.get("scheme")
            if scheme not in SCHEMA["fields"]["legal_basis"]["schemes"]:
                issues.append(f"legal_basis.scheme '{scheme}' is not in the scheme enum")
            out.append({"scheme": scheme, "id": b.get("id")})
        return (out or "n/a"), issues
    if allowed:
        values = value if isinstance(value, list) else [value]
        ok = []
        for v in values:
            if v in allowed:
                ok.append(v)
            else:
                issues.append(f"{field}: '{v}' is not in the enum - value quarantined")
        if not ok:
            return None, issues
        return (ok if field in MULTI else ok[0]), issues
    return value, issues


def validate(fields: dict, fm: dict) -> list[str]:
    issues = []
    # temporal profile follows from the FINAL doc_type, so this must run after the agent merge.
    # Only rule-bearing documents get a validity window; a client note or a ticket is instanced.
    statute = {"eu_reg", "law", "kb", "cao", "be_pc", "fr_idcc", "de_tarif", "it_ccnl", "es_convenio",
               "nl_cao", "ch_gav", "at_kv", "lu_cct", "case"}
    basis = fields.get("legal_basis")
    has_statute = any(isinstance(b, dict) and b.get("scheme") in statute for b in basis) if isinstance(basis, list) else False
    if fields.get("temporal_type") == "evergreen" and (
            fields.get("doc_type") == "legal_summary"
            or (fields.get("doc_type") == "reference" and has_statute)):
        fields["temporal_type"] = "law_window"
        fm["temporal_type"] = {"by": "derived", "confidence": 0.55,
                               "evidence": "legal summary / legal basis => the rules it describes change over time"}
    for field in list(fields):
        if field in ("doc_id", "title", "sector_agreement", "coverage"):
            continue
        value, vissues = check_value(field, fields[field])
        fields[field] = value
        issues += vissues

    # derived
    if fields.get("rule_level"):
        fields["precedence"] = min(LEVEL_ORDER.index(l) + 1 for l in fields["rule_level"] if l in LEVEL_ORDER) \
            if any(l in LEVEL_ORDER for l in fields["rule_level"]) else None
        fm["precedence"] = {"by": "derived", "confidence": 0.8, "evidence": "min(rule_level)"}

    # consistency rules from the schema (the system's real value-add)
    if fields.get("client_id") and fields.get("visibility") not in ("client", "team"):
        issues.append("visibility: client-scoped document must not be visible to everyone")
        fields["visibility"] = "client"
    if not fields.get("client_id") and fields.get("visibility") in ("client", "team"):
        issues.append("visibility: client-only visibility without a resolved client - downgraded to 'all'")
        fields["visibility"] = "all"
    if fields.get("doc_type") == "comms" and fields.get("bindingness") == "binding":
        issues.append("bindingness: a mail/minutes/ticket cannot be binding")
        fields["bindingness"] = "advisory"
    if fields.get("temporal_type") == "evergreen" and fields.get("valid_from"):
        issues.append("valid_from: set for an evergreen document - move it to last_reviewed")
        fields["valid_from"] = None
    if fields.get("temporal_type") == "law_window" and not fields.get("valid_from"):
        issues.append("valid_from: law_window without a validity start - needs review")
    if fields.get("temporal_type") == "evergreen" and not fields.get("last_reviewed"):
        issues.append("last_reviewed: missing for an evergreen document")
    if fields.get("lang") and fields["lang"] not in ("nl", "fr", "en", "de", "it", "es"):
        issues.append(f"lang: '{fields['lang']}' outside the supported set")
    for required in SCHEMA["required"]:
        if required not in fields or fields[required] in (None, "", []):
            issues.append(f"{required}: required field is empty")
    return issues


# ---------------------------------------------------------------- pipeline

def classify(text: str, meta: dict, send=None, backend: str = "offline") -> dict:
    text = ingest(text)
    if meta.get("doc_id") is None:
        meta["doc_id"] = re.sub(r"[^a-z0-9]+", "-", (meta.get("name") or "doc").lower())[:60].strip("-")
    else:
        meta["doc_id"] = str(meta["doc_id"])
    fields, fm = rules_pass(text, meta)
    disagreements: list[str] = []

    if send is not None:
        prior = {k: v for k, v in fields.items() if k not in ("doc_id", "title")}
        reply = agent(send, meta, text, prior)
        if reply is None:
            meta["agent_error"] = "model did not return parseable JSON - kept the rules prior"
        else:
            for field, value in (reply.get("fields") or {}).items():
                if field in PROTECTED and fields.get(field) not in (None, "", []):
                    continue
                if field in INHERITED and fields.get(field) not in (None, "", []):
                    continue
                conf = (reply.get("confidence") or {}).get(field, 0.6)
                prev = fm.get(field)
                # a sure rule is not overruled by a hesitant model
                if (prev and prev["by"] == "rule" and prev["confidence"] >= 0.55
                        and conf < 0.7 and value != fields.get(field)):
                    disagreements.append(
                        f"{field}: kept '{fields.get(field)}' (rule, p={prev['confidence']}) "
                        f"over agent '{value}' (p={conf}) - review")
                    continue
                fields[field] = value
                fm[field] = {"by": "agent", "confidence": conf,
                             "evidence": str((reply.get("evidence") or {}).get(field) or "")[:160]}
            fields["summary"] = reply.get("summary")
            fields["answerable_questions"] = reply.get("answerable_questions")

    issues = inherit_from_client(fields, fm)
    issues += validate(fields, fm)
    issues += disagreements

    low_conf = [k for k, v in fm.items() if v["by"] == "agent" and v["confidence"] < 0.5]
    if low_conf:
        issues.append("low agent confidence: " + ", ".join(sorted(low_conf)))
    unknown_assigned = [k for k, v in fields.items() if isinstance(v, str) and v.lower() in ("unknown", "none", "general")]
    if unknown_assigned:
        issues.append("schema forbids literal 'unknown'/'none': " + ", ".join(unknown_assigned))

    return {
        "doc_id": fields.get("doc_id"),
        "title": fields.get("title"),
        "source": meta,
        "fields": fields,
        "field_meta": fm,
        "needs_review": sorted(set(issues)),
        "schema_version": SCHEMA.get("version", 2),
        "backend": backend,
        "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


BIND_MAP = {"b": "binding", "a": "advisory", "p": "practice", "n": "consultative"}
LEVEL_MAP = {"fed": "federal", "federal": "federal", "eu": "eu", "regional": "regional", "sector": "sector",
             "company": "company", "works_council": "works_council", "client": "client", "individual": "individual"}
WC_ALIAS = {"student": "jobstudent", "uitzend": "uitzendkracht", "exec": "executive", "exemp": "ex_employee"}


def _norm(value, field):
    """Normalise v1 reference values to the v2 vocabulary before comparing."""
    values = value if isinstance(value, list) else ([] if value in (None, "n/a", "-") else [value])
    if field == "bindingness":
        return [BIND_MAP.get(v, v) for v in values]
    if field == "rule_level":
        return sorted({LEVEL_MAP[v] for v in values if v in LEVEL_MAP})
    if field == "worker_category":
        return sorted({WC_ALIAS.get(v, v) for v in values if v != "multi"})
    return values


def compare(reference_path: Path, records: list[dict]) -> None:
    """Agreement against the hand-tagged reference - the only honest quality number.
    The reference was written under v1, so short codes and scalars are normalised first."""
    ref = {d["id"]: d for d in json.loads(reference_path.read_text())}
    by_id = {int(r["source"]["doc_id"]): r for r in records
             if str(r["source"].get("doc_id", "")).isdigit()}
    lists = {"process_domain", "worker_category", "pay_component", "rule_level"}
    shared = sorted(set(by_id) & set(ref))
    print(f"\nagreement vs {reference_path.name} ({len(shared)} docs, reference normalised to v2)")
    for field in ["doc_type", "bindingness", "temporal_type"] + sorted(lists):
        hit, misses = 0, []
        for i in shared:
            got = by_id[i]["fields"].get(field)
            want = ref[i].get(field)
            g, w = set(_norm(got, field)), set(_norm(want, field))
            ok = (g == w) or bool(g & w)
            if field in ("bindingness", "doc_type", "temporal_type"):
                ok = g == w
            hit += ok
            if not ok and len(misses) < 3:
                misses.append(f"{i}: got={got!r} want={want!r}")
        if shared:
            print(f"  {field:18} {hit}/{len(shared)}  {100*hit//len(shared):3d}%   {' | '.join(misses)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Classify organisational knowledge into the SD Worx schema.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path)
    src.add_argument("--text")
    src.add_argument("--stdin", action="store_true")
    src.add_argument("--dir", type=Path, help="classify every supported text file in a folder")
    src.add_argument("--demo", type=int, metavar="N", help="classify the first N seed docs from schema/documents.json")
    ap.add_argument("--backend", choices=["offline", "openai"], default="openai" if os.environ.get("OPENAI_API_KEY") else "offline")
    ap.add_argument("--model", default=os.environ.get("CLASSIFY_MODEL", "gpt-4o-mini"))
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--compare", type=Path, help="reference tags to measure agreement against")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    load_env()
    send = None
    if args.backend == "openai":
        send = make_openai_send(args.model, use_tools=not args.no_tools)

    jobs: list[tuple[str, dict]] = []
    if args.text:
        jobs.append((args.text, {"name": "pasted text", "path": "<stdin>", "mtime": TODAY}))
    elif args.stdin:
        jobs.append((sys.stdin.read(), {"name": "stdin", "path": "<stdin>", "mtime": TODAY}))
    elif args.file:
        jobs.append((read_source(args.file),
                     {"name": args.file.stem, "path": str(args.file), "mtime": MTIME(args.file)}))
    elif args.dir:
        for p in sorted(args.dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
                jobs.append((read_source(p), {"name": p.stem, "path": str(p), "mtime": MTIME(p)}))
    else:
        for d in json.loads((SCHEMA_DIR / "documents.json").read_text())[: args.demo]:
            jobs.append((f"{d['name']}. {d['summary']}",
                         {"doc_id": d["id"], "name": d["name"], "path": f"intranet/{d['section']}/", "mtime": TODAY}))

    records = []
    for payload, meta in jobs:
        rec = classify(payload, meta, send=send, backend=args.backend)
        records.append(rec)
        if not args.quiet:
            fl = rec["fields"]
            print(f"{rec['doc_id'][:34]:34} {str(fl.get('doc_type')):17} {','.join(fl.get('process_domain') or [])[:34]:34} "
                  f"{str(fl.get('bindingness')):10} {str(fl.get('client_id') or '-'):4} "
                  f"review={len(rec['needs_review'])}")

    OUT_DIR.mkdir(exist_ok=True)
    for rec in records:
        (OUT_DIR / f"{rec['doc_id']}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    with (OUT_DIR / "index.jsonl").open("a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    review = sum(1 for r in records if r["needs_review"])
    print(f"\n{len(records)} classified -> {OUT_DIR}  ({review} flagged for human review)")
    if args.compare:
        compare(args.compare, records)


if __name__ == "__main__":
    main()
