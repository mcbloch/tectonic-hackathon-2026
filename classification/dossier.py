#!/usr/bin/env python3
"""Client dossier builder.

Input : a folder of documents about ONE client (instructions, minutes, SLA, tickets, sector news).
Output: the four blocks the payroll team works from -
        exceptions/unique rules, people/owners, open issues, deadlines.

Every entry must cite a source document; entries that cite a document that was not supplied are
dropped by the validator, so the dossier cannot invent facts.

    python3 dossier.py --client C2 --dir inbox/chemco
    python3 dossier.py --client C2 --dir inbox/chemco --backend openai
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import classify as C                      # reuse ingestion, client master, agent loop, LLM adapter

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "clients"
PROMPT = (ROOT / "dossier_prompt.md").read_text()

SEVERITY = ["high", "medium", "low"]
I_STATUS = ["open", "waiting", "blocked", "done"]
D_TYPE = ["declaration", "payment", "legal", "contract", "review", "meeting", "delivery"]
RECURRENCE = ["one_off", "monthly", "quarterly", "annual"]
SIDE = ["client", "sdworx", "third_party"]

ROLE_WORDS = (r"contact|verantwoordelijke|verantwoordelijk|manager|consultant|director|directeur|voorzitter|"
              r"afgevaardigde|verslag|owner|lead|adviseur|assistente|service manager|aanspreekpunt")
PERSON = r"([A-Z][a-zà-ÿ]+(?:[ '’\-][A-Za-zà-ÿ'’\-]+){1,3})"
ISSUE_WORDS = r"open|wacht op|blocked|geen akkoord|nog geen|te bevestigen|probleem|fout|issue|correctie|onduidelijk"
EXC_WORDS = r"afwijk|in plaats van|uitzondering|instead of|deviat|specifiek voor"
DEADLINE_WORDS = r"uiterlijk|ten laatste|v[óo][óo]r \d|deadline|binnen \d|vanaf|ingangsdatum|treedt in werking"


STATIC_SUFFIX = ".classification.json"


def load_docs(folder: Path) -> list[dict]:
    docs = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in C.TEXT_SUFFIXES and not p.name.endswith(STATIC_SUFFIX):
            docs.append({"doc_id": p.stem, "title": p.stem.replace("-", " "), "text": C.read_source(p)})
    return docs


def load_static(folder: Path, client_id: str) -> dict | None:
    """Hand-written ground truth for a client, if it exists. Used until the classifier lands."""
    for candidate in (folder / f"{client_id}{STATIC_SUFFIX}", folder.parent / f"{client_id}{STATIC_SUFFIX}"):
        if candidate.exists():
            return json.loads(candidate.read_text())
    return None


def norm_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", raw)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-zéûôà]+)\s+(\d{4})", raw)
    if m and C.MONTHS.get(m.group(2).lower()):
        return f"{m.group(3)}-{C.MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def schedule_of(line: str) -> tuple[str | None, str]:
    """Recurring payroll obligations rarely have a fixed date - capture the schedule text instead."""
    m = re.search(r"(\d+e\s+werkdag[^,.;]{0,30}|laatste\s+werkdag[^,.;]{0,20}|\d+\s+dagen\s+na[^,.;]{0,30}|"
                  r"per\s+kwartaal|maandelijks|per\s+maand|jaarlijks|per\s+jaar|elke\s+maand)", line, re.I)
    low = line.lower()
    recur = ("monthly" if ("maand" in low or "werkdag" in low) else
             "quarterly" if "kwartaal" in low else
             "annual" if ("jaar" in low or "jaarlijks" in low) else "one_off")
    return (m.group(1).strip() if m else None), recur


def rules_candidates(docs: list[dict]) -> dict:
    """Cheap deterministic sweep - feeds the agent as a prior and backs the offline mode."""
    out = {"dates": [], "people": [], "issue_lines": [], "exception_lines": [], "deadline_lines": []}
    for d in docs:
        for line in d["text"].splitlines():
            line = line.strip(" -•\t")
            if len(line) < 12:
                continue
            stamp = None
            m = re.search(r"(\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{1,2}\s+[A-Za-zéûôà]+\s+\d{4}|\d{4}-\d{2}-\d{2})", line)
            if m:
                stamp = norm_date(m.group(1))
            for name in re.findall(PERSON, line):
                # a role title is not a person: "Service manager SD Worx" and "Contact bij vragen" are not names
                if (re.search(ROLE_WORDS, name, re.I) or len(name.split()) < 2
                        or not (re.search(ROLE_WORDS, line, re.I) or "@" in line)):
                    continue
                out["people"].append({"name": name, "line": line[:150], "source_doc": d["doc_id"]})
            if re.search(ISSUE_WORDS, line, re.I):
                out["issue_lines"].append({"text": line[:220], "source_doc": d["doc_id"]})
            if re.search(EXC_WORDS, line, re.I):
                out["exception_lines"].append({"text": line[:220], "source_doc": d["doc_id"]})
            if re.search(DEADLINE_WORDS, line, re.I) and (stamp or re.search(r"(maand|kwartaal|jaar|dag)", line, re.I)):
                sched, recur = schedule_of(line)
                out["deadline_lines"].append({"text": line[:220], "date": stamp, "schedule": sched,
                                              "recurrence": recur, "source_doc": d["doc_id"]})
    seen = set()
    out["people"] = [p for p in out["people"] if not (p["name"] in seen or seen.add(p["name"]))]
    for key in ("issue_lines", "exception_lines", "deadline_lines"):
        unique, seen = [], set()
        for item in out[key]:
            if item["text"] not in seen:
                seen.add(item["text"])
                unique.append(item)
        out[key] = unique
    return out


def offline_dossier(cand: dict, docs: list[dict]) -> dict:
    """No model available: emit what the rules can stand behind, nothing more."""
    def title(text):
        t = re.sub(r"^\s*(\d+[.)]\s*|[a-z][.)]\s*)", "", text)          # strip list numbering
        t = t.split(". ")[0]
        return t[:160] if len(t.strip()) >= 15 else text[:160]
    return {
        "exceptions": [{"statement": c["text"], "overrides": None, "site": None, "worker_group": None,
                        "authority": "client_instruction", "valid_from": None, "valid_to": None,
                        "source_doc": c["source_doc"]} for c in cand["exception_lines"][:6]],
        "people": [{"name": p["name"], "role": None, "side": "client", "contact": None,
                    "source_doc": p["source_doc"]} for p in cand["people"][:8]],
        "issues": [{"title": title(c["text"]), "detail": c["text"], "severity": "medium",
                    "status": "waiting" if re.search(r"wacht op|nog geen|te bevestigen", c["text"], re.I) else "open",
                    "owner_name": None, "raised_on": None, "next_action": None,
                    "source_doc": c["source_doc"]} for c in cand["issue_lines"][:6]],
        "deadlines": [{"what": title(c["text"]), "date": c["date"], "schedule": c.get("schedule"),
                       "type": "declaration" if re.search(r"aangifte|dmfa|dimona|fiche", c["text"], re.I)
                               else "payment" if re.search(r"betal|bijdrage|premie", c["text"], re.I)
                               else "legal" if re.search(r"cao|indexat|wet", c["text"], re.I) else "review",
                       "recurrence": c.get("recurrence", "one_off"), "owner_name": None,
                       "source_doc": c["source_doc"]} for c in cand["deadline_lines"][:8]],
    }


def scalar(value, notes: list[str], where: str):
    """One tag = one value. A list from the model is a schema violation: keep the first, report it."""
    if isinstance(value, list):
        notes.append(f"{where}: model returned a list - kept the first value, tag must be single-valued")
        value = value[0] if value else None
    if isinstance(value, dict):
        notes.append(f"{where}: model returned an object - dropped, tag must be single-valued")
        return None
    if isinstance(value, str):
        value = value.strip()
        if "," in value and len(value) < 60:          # "Antwerpen, Gent" is two values in one string
            notes.append(f"{where}: '{value}' looks like two values packed into one string - review")
        return value or None
    return value


def one_of(value, allowed, default):
    v = (value or "").strip().lower().replace(" ", "_")
    return v if v in allowed else default


def validate(dossier: dict, doc_ids: set[str]) -> tuple[dict, list[str]]:
    """The only writer. Drops unsourced entries and normalises every field."""
    notes: list[str] = []
    clean: dict = {}
    dropped = 0

    def keep(entry):
        nonlocal dropped
        if entry.get("source_doc") not in doc_ids:
            dropped += 1
            return False
        return True

    exceptions = []
    seen_exc = set()
    for e in filter(keep, dossier.get("exceptions") or []):
        stmt = (e.get("statement") or "").strip()
        if not stmt:
            continue
        site = scalar(e.get("site"), notes, "exceptions.site")
        group = scalar(e.get("worker_group"), notes, "exceptions.worker_group")
        key = (stmt[:120].lower(), str(site).lower(), str(group).lower())
        if key in seen_exc:                      # same fact, same scope: one entry
            continue
        seen_exc.add(key)
        exceptions.append({"id": f"e{len(exceptions)+1}", "statement": stmt[:300],
                           "overrides": (e.get("overrides") or None),
                           "site": site, "worker_group": group,
                           "authority": one_of(e.get("authority"),
                                               ["client_instruction", "client_collective", "sector_agreement", "law"],
                                               "client_instruction"),
                           "valid_from": norm_date(e.get("valid_from")), "valid_to": norm_date(e.get("valid_to")),
                           "source_doc": e["source_doc"]})
    clean["exceptions"] = exceptions

    people = []
    for p in filter(keep, dossier.get("people") or []):
        name = (p.get("name") or "").strip()
        if not name or len(name.split()) < 2:          # roles without a name are not people
            continue
        if any(x["name"].lower() == name.lower() for x in people):
            continue
        people.append({"id": f"p{len(people)+1}", "name": name[:80], "role": (p.get("role") or None),
                       "side": one_of(p.get("side"), SIDE, "client"),
                       "contact": (p.get("contact") or None), "source_doc": p["source_doc"]})
    clean["people"] = people

    def pid(name):
        if not name:
            return None
        for p in people:
            if p["name"].lower() in str(name).lower() or str(name).lower() in p["name"].lower():
                return p["id"]
        return None

    issues = []
    for i in filter(keep, dossier.get("issues") or []):
        title = (i.get("title") or "").strip()
        if not title:
            continue
        if any(x["title"].lower()[:60] == title.lower()[:60] for x in issues):
            continue
        issues.append({"id": f"i{len(issues)+1}", "title": title[:160], "detail": (i.get("detail") or None),
                       "severity": one_of(i.get("severity"), SEVERITY, "medium"),
                       "status": one_of(i.get("status"), I_STATUS, "open"),
                       "owner": pid(i.get("owner_name")), "owner_name": (i.get("owner_name") or None),
                       "raised_on": norm_date(i.get("raised_on")), "next_action": (i.get("next_action") or None),
                       "source_doc": i["source_doc"]})
    clean["issues"] = issues

    deadlines = []
    for d in filter(keep, dossier.get("deadlines") or []):
        what = (d.get("what") or "").strip()
        if not what:
            continue
        date = norm_date(d.get("date"))
        schedule = (d.get("schedule") or None)
        if not date and not schedule:
            notes.append(f"deadline '{what[:50]}' has neither a date nor a schedule - kept, needs review")
        deadlines.append({"id": f"d{len(deadlines)+1}", "what": what[:160], "date": date, "schedule": schedule,
                          "type": one_of(d.get("type"), D_TYPE, "review"),
                          "recurrence": one_of(d.get("recurrence"), RECURRENCE, "one_off"),
                          "owner": pid(d.get("owner_name")), "owner_name": (d.get("owner_name") or None),
                          "source_doc": d["source_doc"]})
    clean["deadlines"] = deadlines

    if dropped:
        notes.append(f"{dropped} entries dropped: source_doc was not one of the supplied documents")
    for block, label in (("exceptions", "exceptions"), ("people", "people"),
                         ("issues", "open issues"), ("deadlines", "deadlines")):
        if not clean[block]:
            notes.append(f"no {label} found in these documents")
    return clean, notes


def build(client_id: str, docs: list[dict], send=None, backend: str = "offline", static: dict | None = None) -> dict:
    cand = rules_candidates(docs)
    if static is not None:
        blocks = {k: (static.get(k) or []) for k in ("exceptions", "people", "issues", "deadlines")}
        engine = "static classification (hand-written)"
    elif send is None:
        blocks = offline_dossier(cand, docs)
        engine = "rules-only"
    else:
        payload = "\n\n".join(f"### doc_id: {d['doc_id']}\n{d['text'][:6000]}" for d in docs)
        meta = {"doc_id": client_id, "name": client_id, "path": str(ROOT / "inbox")}
        client = C.CLIENTS.get(client_id, {})
        prior = {"client_master": client, "candidates": cand}
        reply = C.agent(send, meta, payload, prior, max_steps=2, system=PROMPT)
        if reply is None:
            blocks = offline_dossier(cand, docs)
            engine = "rules-only (agent returned nothing parseable)"
        else:
            blocks = {k: (reply.get(k) or []) for k in ("exceptions", "people", "issues", "deadlines")}
            engine = f"agent ({backend})"
    clean, notes = validate(blocks, {d["doc_id"] for d in docs})
    return {
        "client_id": client_id,
        "client": C.CLIENTS.get(client_id, {}),
        "documents": [{"doc_id": d["doc_id"], "title": d["title"]} for d in docs],
        "exceptions": clean["exceptions"], "people": clean["people"],
        "issues": clean["issues"], "deadlines": clean["deadlines"],
        "notes": notes,
        "engine": engine,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def render(d: dict) -> str:
    c = d.get("client") or {}
    out = [f"CLIENT DOSSIER  {d['client_id']}  {c.get('name','')}  "
           f"[{c.get('country','?')} · {'/'.join(x for x in [str(c.get('pc_code') or '')] if x) or 'no pc'} · "
           f"{c.get('coverage_tier','?')}/{c.get('pack_status','?')}]  engine={d['engine']}",
           f"documents: {', '.join(x['doc_id'] for x in d['documents'])}", ""]
    sections = [("EXCEPTIONS / UNIQUE RULES", "exceptions", lambda e: (
                    f"    {e['id']}  {e['statement'][:110]}"
                    f"{' | overrides: ' + e['overrides'][:60] if e['overrides'] else ''}"
                    f"  [{e['authority']}{', site=' + e['site'] if e.get('site') else ''}"
                    f"{', group=' + e['worker_group'] if e.get('worker_group') else ''}"
                    f"{', from ' + e['valid_from'] if e['valid_from'] else ''}"
                    f"{', to ' + e['valid_to'] if e.get('valid_to') else ''}]"
                    f"  <- {e['source_doc']}")),
                ("PEOPLE / OWNERS", "people", lambda p: (
                    f"    {p['id']}  {p['name']:24} {str(p['role'] or '-'):42} {p['side']:11} <- {p['source_doc']}")),
                ("OPEN ISSUES", "issues", lambda i: (
                    f"    {i['id']}  [{i['severity']:6}/{i['status']:8}] {i['title'][:80]}"
                    f"{' | owner: ' + i['owner_name'] if i['owner_name'] else ''} <- {i['source_doc']}")),
                ("DEADLINES", "deadlines", lambda x: (
                    f"    {x['id']}  {str(x['date'] or x['schedule'] or '?'):20} {x['type']:12} {x['recurrence']:9} "
                    f"{x['what'][:64]}{' | owner: ' + x['owner_name'] if x['owner_name'] else ''} <- {x['source_doc']}"))]
    for title, key, fmt in sections:
        out.append(f"{title}  ({len(d[key])})")
        out += [fmt(x) for x in d[key]] or ["    - nothing found"]
        out.append("")
    if d["notes"]:
        out.append("NOTES")
        out += [f"    ! {n}" for n in d["notes"]]
    return "\n".join(out)


def tag_rows(d: dict) -> list[dict]:
    """The indexable form: one row per (entry, tag, value). Guarantees one value per tag,
    and explodes any list a future field might still carry into separate rows."""
    rows = []
    base = {"client_id": d["client_id"], "generated_at": d["generated_at"]}
    for block in ("exceptions", "people", "issues", "deadlines"):
        for entry in d[block]:
            for tag, value in entry.items():
                if tag == "id" or value in (None, "", []):
                    continue
                for v in (value if isinstance(value, list) else [value]):
                    rows.append({**base, "entry": entry["id"], "block": block, "tag": tag,
                                 "value": v, "source_doc": entry.get("source_doc")})
    return rows


def portfolio(dossiers: list[dict]) -> str:
    """One row per client - what a service manager actually opens on a Monday morning."""
    rows = []
    for d in dossiers:
        c = d.get("client") or {}
        high = [i for i in d["issues"] if i["severity"] == "high" and i["status"] != "done"]
        dates = sorted(x for x in (dd["date"] for dd in d["deadlines"]) if x)
        rows.append((dates[0] if dates else "9999", d["client_id"], c.get("name", ""),
                     len(d["exceptions"]), len(d["people"]), len(d["issues"]), len(high),
                     dates[0] if dates else "-", dates[-1] if dates else "-"))
    rows.sort()
    head = f"{'client':7} {'name':22} {'exc':>3} {'ppl':>3} {'iss':>3} {'high':>4}  {'next deadline':13} {'last':12}"
    out = ["CLIENT PORTFOLIO", head, "-" * len(head)]
    for _, cid, name, exc, ppl, iss, high, nxt, last in rows:
        out.append(f"{cid:7} {name[:22]:22} {exc:>3} {ppl:>3} {iss:>3} {high:>4}  {nxt:13} {last:12}")
    return "\n".join(out)


def write_outputs(d: dict) -> tuple[Path, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    j = OUT / f"{d['client_id']}.json"
    t = OUT / f"{d['client_id']}.tags.jsonl"
    j.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    t.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in tag_rows(d)) + "\n")
    return j, t


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a client dossier from a folder of documents.")
    ap.add_argument("--client", help="client id from clients.json (e.g. C2)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", type=Path, help="folder with this client's documents")
    src.add_argument("--all", action="store_true", help="treat every subfolder of inbox/ as one client")
    ap.add_argument("--inbox", type=Path, default=ROOT / "inbox")
    ap.add_argument("--backend", choices=["offline", "openai"],
                    default="openai" if __import__("os").environ.get("OPENAI_API_KEY") else "offline")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--reclassify", action="store_true",
                    help="ignore <client>.classification.json and run the extractor instead")
    args = ap.parse_args()

    C.load_env()
    send = C.make_openai_send(args.model, use_tools=not args.no_tools) if args.backend == "openai" else None
    OUT.mkdir(parents=True, exist_ok=True)

    if args.all:
        folders = [(p.name, p) for p in sorted(args.inbox.iterdir()) if p.is_dir()]
        if not folders:
            sys.exit(f"no client folders under {args.inbox}")
        dossiers = []
        for cid, folder in folders:
            docs = load_docs(folder)
            if not docs:
                continue
            static = None if args.reclassify else load_static(folder, cid)
            d = build(cid, docs, send=send, backend=args.backend, static=static)
            write_outputs(d)
            dossiers.append(d)
        print(portfolio(dossiers))
        print(f"\n{len(dossiers)} dossiers (+ .tags.jsonl) -> {OUT}")
        return

    docs = load_docs(args.dir)
    if not docs:
        sys.exit(f"no supported documents under {args.dir}")
    static = None if args.reclassify else load_static(args.dir, args.client)
    dossier = build(args.client, docs, send=send, backend=args.backend, static=static)
    j, t = write_outputs(dossier)
    print(render(dossier))
    print(f"\nwritten -> {j}\n           {t}")


if __name__ == "__main__":
    main()
