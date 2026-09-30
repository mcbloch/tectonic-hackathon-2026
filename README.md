# Digital Secretary — client memory for SD Worx payroll

SD Worx runs payroll for clients on a consultancy / managed-payroll basis. This is a hackathon
proof of concept: a per-client **digital secretary** that keeps track of what is actually true for
that client right now.

## The problem

Knowledge about a client is scattered across Teams, emails, documents, notes and systems — and
sometimes it only exists in one person's memory. When that person is absent, whoever takes over has
to reconstruct months of context before they can safely run payroll. Microsoft already helps SD Worx
*find* information; it does not keep track of whether that information is still correct.

## What we build

Not another chatbot. Not another knowledge base. A layer that continuously keeps tabs on each client:

- deadlines, exceptions, client-specific rules, open issues, decisions, owners, recent changes.
- When new information arrives — a document, a Teams message or an email — it is checked against what
  is already known.
- On a conflict, the secretary asks the human: **is this a new rule, a one-off exception, or is the
  old information still correct?**

The secretary asks for clarification throughout the lifetime of the engagement, not only at the
moment information is needed, so the problem is handled before it occurs.

## Architecture

- **SharePoint** extended with **SPFx** — client workspace / memory surface.
- **Teams** with a **Message Extension** — capture answers where the conversation happens.
- **Microsoft 365 Declarative Agent** — exposes the client-memory engine to Copilot.
- **Graph / Work IQ** — connects the engine to the existing Microsoft data (Teams, Outlook,
  SharePoint/OneDrive).
- **Client-memory engine** (`classification/`) — turns a folder of client documents into the blocks
  a payroll consultant works from: unique rules/exceptions, people & owners, open issues, deadlines.
  Every entry must cite a source document; entries citing a document that was not supplied are
  dropped, so the dossier cannot invent facts.

## First scenario: taking over an absent colleague's client

1. A payroll consultant has to take over a client from a colleague who is absent.
2. Instead of searching Teams, SharePoint, emails and old notes, they open the client's Digital
   Secretary.
3. They ask: **"What do I need to know before I run payroll?"**
4. The secretary returns the current client state: deadlines, client-specific exceptions, open
   issues, recent changes, and who owns each decision.
5. The consultant can take over safely — without reconstructing months of context.

**Must work:** the question above is answered from the client state with sources, and conflicts
between sources are surfaced instead of silently averaged.

## Demo path in this repo

| Piece | Path | What it does |
| --- | --- | --- |
| Teams-style UI + Copilot pane | `teams/` | Client workspace shell with Copilot chat, Files and Mail views |
| Backend + data API | `server.py` | Serves the UI, `/api/files`, `/api/mail`, `/api/memory`, `/api/teams`, `/api/document/<id>`, and the Copilot chat endpoint |
| Copilot context builder | `teams/server.py` | Assembles client memory + documents + Teams + Outlook + seeded conflicts into the model context |
| Client-memory engine | `classification/` | `dossier.py` builds the four dossier blocks from a client folder; `classify.py` tags single documents |
| Synthetic client pack | `mock-data/` | Round 2 pack for a fictional Proximus Belgium client: Word/Excel/PDF source documents, 15 Outlook mails, 100 Teams messages, seeded conflicts, continuity questions, client memory |

`mock-data/` is synthetic. It is not real Proximus, SD Worx, Microsoft Teams, Outlook, SharePoint or
OneDrive content. See `mock-data/README.md` for the full layout.

## Run

Python 3, standard library only.

```bash
cp teams/.env.example teams/.env   # or put API_KEY in .env at the repo root
python3 server.py                  # http://127.0.0.1:8000/
```

The Copilot pane calls any OpenAI-compatible `/chat/completions` endpoint. Defaults:
`https://opencode.ai/zen/go/v1` with `deepseek-v4-flash`; override with `COPILOT_BASE_URL`,
`COPILOT_API_KEY`, `COPILOT_MODEL`. The process environment always wins over `.env`.

Client-memory engine:

```bash
cd classification && python3 dossier.py --client C2 --dir inbox/C2
```

Writes `classification/out/clients/C2.json` (exceptions, people/owners, open issues, deadlines —
every entry citing its source document). `--backend openai` switches from the offline rule engine
to the LLM; see `classification/.env.example`.

## The team

- Mihaly Csonka
- Rostyslav Fedorov
- Maxime Bloch
- Robert Akhmerov

## Round 1

Check-in opens Sep 30, 2026, 5:00 PM GMT+2

https://builderbase.com/track-dashboard/sd-worx-ydpk/event-site

### Venue

692 Diestsesteenweg
Leuven 3010
Vlaanderen, BE

https://www.google.com/maps/search/?api=1&query=50.88767110000001,4.7621158&query_place_id=ChIJZ-8vmA5nUcRJnQjXb6H4jo
