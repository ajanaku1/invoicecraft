# InvoiceCraft — Plan & Permissions

## Autonomy level

**High** — run end-to-end through all phases; stop only at the hard checkpoints and on blockers.

Edit this file to tune stop points.

## Stop-and-ask checkpoints (must pause)

1. **Frontend proposal gate** — after generating 3 HTML direction files, present them via AskUserQuestion and wait for selection before any dashboard code
2. **OKX.AI registration** — before submitting ASP for listing review (outward-facing, irreversible once live)
3. **X post** — before posting the demo on X with #OKXAI (outward-facing, public)
4. **Google form** — before submitting the hackathon form (irreversible)
5. **Deployment** — before deploying to production HTTPS endpoint
6. **Scope change** — any work not in `Goal.md`, or a decision that contradicts it
7. **Secrets/credentials** — if OKX API keys, wallet keys, or deployment credentials are needed and not available

## Proceed without asking (green light)

- Scaffolding project structure, writing `spec.md`
- Writing and running tests
- Implementing invoice generation, PDF engine, tax calculation
- Implementing x402 middleware
- Running `code-reviewer` + `simplify` passes
- Local commits to a working branch
- Writing reports/ notes for upstream issues
- Polishing copy with `humanizer`

## Unexpected-hurdle protocol

When blocked: stop, state the situation in one line, present concrete options via AskUserQuestion with a recommended default. Make one reasonable attempt before escalating. Don't loop on the same failure.

## Resuming

After any stop, continue from the current task in `prompt.md`'s task list without re-confirming earlier completed work.
