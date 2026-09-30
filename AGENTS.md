# AGENTS.md

This is a time-boxed hackathon project. Optimize for a working demo, not a polished product.

## Project
<!-- Fill in once the topic is announced: 2-3 sentences on what we're building and for whom. -->
TBD

## Demo goal
<!-- The exact flow we will show the judges. Keep it to 3-5 steps. -->
1. TBD
2. TBD
3. TBD

**Must work:** TBD (the single feature the demo cannot live without)

## Stack and commands
<!-- Fill in once chosen. If this is still empty, inspect the repo (package.json,
pyproject.toml, requirements.txt, go.mod, etc.) to infer it, and tell the human what you inferred. -->
- Stack: TBD
- Install: `TBD`
- Run: `TBD`
- Test: `TBD`

## Priorities
1. The demo path works end-to-end before anything else.
2. Then reliability of the demo path (no crashes, sensible empty and error states).
3. Then visuals and polish.
4. Everything else is out of scope unless the human asks.

When two options are close, pick the simpler one and move on.

## Working rules
- Build the thinnest vertical slice first (UI -> logic -> data), then widen it.
- Mock or stub external services and slow dependencies until the main flow works, then swap in real ones.
- Prefer the standard library and dependencies already in the project. Ask before adding a new one.
- Keep structure flat and simple. Functions over abstractions. No premature generalization.
- Define clear data shapes (types, schemas, or typed dicts) at the boundaries between components, so work in parallel doesn't collide.
- Tests only for logic that would break the demo if wrong. Skip the rest.
- Seed or fixture data is fine and encouraged so the demo is repeatable.
- Comments only where the code isn't obvious. No separate docs during the event.

## Boundaries
- Stay within the files relevant to the task. Don't refactor, rename, or reformat unrelated code.
- Don't rewrite working code for style.
- Don't change the demo path, shared data shapes, or project structure without flagging it first.
- Never commit secrets. Keep real values in `.env` (gitignored) and maintain a `.env.example` with placeholder values.
- Don't run destructive commands (force-push, dropping data, deleting directories) without asking.
- Teammates may be editing other parts of the repo at the same time. Keep changes small and focused so merges stay easy.

## When stuck
1. Read the actual error and logs before guessing.
2. Try the smallest fix first.
3. If the same problem survives two attempts, stop. Summarize what you tried, what you think is wrong, and the options you see, then ask the human.
4. If a feature is taking much longer than expected, propose a cut-down or faked version that still demos well.

## Finishing a task
- Briefly state what changed and how to verify it (command to run or thing to click).
- Note anything hardcoded, mocked, or left unfinished so nothing surprises us at demo time.
