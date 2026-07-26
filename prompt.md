# InvoiceCraft — Build Prompt

Build InvoiceCraft per `Goal.md` (source of truth). No scope creep beyond what `Goal.md` defines.

## What you are building

A2MCP endpoint: POST natural language job description → x402 payment challenge (0.50 USDT) → invoice JSON + PDF. Core journey: buyer calls → pays on X Layer → gets professional PDF invoice → sends to client. Demo dashboard for browser-based testing. See `Goal.md` for full scope.

### Sponsor integration map

| Sponsor tech | Where it's load-bearing | Feature that breaks without it |
|---|---|---|
| OKX.AI A2MCP | Service registration, discovery, task flow | ASP doesn't exist on marketplace |
| x402 (OKX Payment SDK) | Pay-per-call billing on every endpoint call | No revenue, no payment challenge returned |
| X Layer (eip155:196) | Settlement chain for USDT0 payments | Payments can't settle onchain |
| OKX Agentic Wallet | Identity, receiving address for payments | Can't register ASP or receive funds |

Integration must be structural — the build's spine. If any sponsor technology could be removed without breaking the core journey, redesign until it can't.

## Required skills & install check

| Skill | Purpose | Install status |
|---|---|---|
| `backend-architect` | FastAPI service architecture, endpoint design | INSTALLED |
| `api-design-principles` | REST API patterns, x402 challenge structure | INSTALLED |
| `frontend-design` | Demo dashboard UI | INSTALLED |
| `test-driven-development` | Invoice generation tests, endpoint tests | INSTALLED |
| `code-reviewer` | Review every change | INSTALLED |
| `simplify` | Post-review cleanup | INSTALLED |
| `humanizer` | User-facing copy, demo script | INSTALLED |

Re-check on a fresh machine:
```bash
set -- backend-architect api-design-principles frontend-design test-driven-development code-reviewer simplify humanizer
for s in "$@"; do
  if find -L "$HOME/.agents/skills" "$HOME/.claude/skills" .skills ./skills \
       -maxdepth 4 -type f -name SKILL.md -path "*/$s/SKILL.md" 2>/dev/null | grep -q .; then
    echo "INSTALLED  $s"
  else
    echo "MISSING    $s"
  fi
done
```

## Task tracking

Use `todaWrite` at the start of each phase. One task = one shippable outcome. Skill invocations live inside tasks.

## Skills to use — non-negotiable

| Phase | Skill | Use it for |
|---|---|---|
| Architecture | `backend-architect` | Design the FastAPI service structure, endpoint contract, x402 middleware placement |
| Architecture | `api-design-principles` | Define the x402 challenge response format, error handling, request/response schemas |
| Build | `test-driven-development` | Write invoice generation tests BEFORE implementation; endpoint contract tests |
| Build | `backend-architect` | Implement the service, PDF generation, tax calculation |
| Build | `api-design-principles` | Ensure x402 compliance, correct 402 response headers |
| UI | `frontend-design` | Generate 3 dashboard direction proposals as standalone HTML files |
| UI | `frontend-design` | Build the selected dashboard direction |
| Every change | `code-reviewer` | Review code for correctness, security, API compliance |
| After review | `simplify` | Clean up redundancies, improve clarity |
| Copy | `humanizer` | Polish demo script, X post copy, service description |

## How the skills compose

### Phase 0 — Architecture (Day 0, ~2 hours)
1. `backend-architect` designs service structure and endpoint contract
2. `api-design-principles` defines x402 challenge format and error schemas
3. Output: `spec.md` with endpoint contract, data models, x402 flow

### Phase 1 — Core Build (Day 0-1, ~6 hours)
1. `test-driven-development` writes tests for invoice generation + endpoint
2. `backend-architect` implements FastAPI service, PDF engine, tax logic
3. `api-design-principles` ensures x402 middleware integration
4. `code-reviewer` + `simplify` after each implementation chunk
5. Output: Working endpoint returning invoice JSON + PDF

### Phase 2 — Frontend (Day 1, ~3 hours)
1. `frontend-design` generates 3 dashboard direction proposals as HTML files
2. User selects direction via AskUserQuestion (see frontend proposal gate below)
3. `frontend-design` builds selected dashboard
4. `code-reviewer` + `simplify` after UI implementation
5. Output: Deployed demo dashboard

### Phase 3 — Marketplace & Demo (Day 1-2, ~3 hours)
1. Register ASP on OKX.AI with correct A2MCP endpoint
2. `humanizer` polishes service description and 90-second demo script
3. Post demo on X with #OKXAI
4. Submit Google form
5. Output: Live ASP, demo posted, form submitted

## Frontend proposal gate

The build has a UI (demo dashboard). Before any frontend code is written:

1. Generate 3 distinct UI direction proposals as standalone `.html` files in `proposals/`:
   - `proposals/option-a.html` — minimal terminal-style (dark, monospace, CLI aesthetic)
   - `proposals/option-b.html` — clean SaaS dashboard (light, cards, modern)
   - `proposals/option-c.html` — invoice-preview focused (shows PDF output prominently)
2. Each file must be self-contained (inline CSS, no build step) and openable in a browser
3. Present the 3 options via AskUserQuestion with file paths
4. Wait for user selection before writing any frontend code
5. Record the choice in the task list

## File layout

```
InvoiceCraft/
├── Goal.md                    # source of truth (this file defers to it)
├── prompt.md                  # this file
├── plan.md                    # autonomy/permissions contract
├── spec.md                    # endpoint contract, data models, x402 flow
├── app/
│   ├── main.py                # FastAPI application
│   ├── invoice.py             # Invoice generation logic
│   ├── pdf_engine.py          # PDF rendering (WeasyPrint or ReportLab)
│   ├── tax.py                 # Tax calculation
│   ├── x402.py                # x402 payment middleware
│   └── models.py              # Pydantic models
├── tests/
│   ├── test_invoice.py        # Invoice generation tests
│   ├── test_endpoint.py       # Endpoint contract tests
│   └── test_x402.py           # x402 challenge tests
├── proposals/
│   ├── option-a.html          # UI direction A
│   ├── option-b.html          # UI direction B
│   └── option-c.html          # UI direction C
├── dashboard/                 # Demo dashboard (selected direction)
│   ├── index.html
│   └── app.js
├── reports/                   # Upstream issue reports
├── Dockerfile                 # Production container
├── requirements.txt           # Python dependencies
├── okx-ai-listing.md          # Marketplace listing copy
├── DEMO_SCRIPT.md             # 90-second walkthrough
└── README.md                  # Project documentation
```

## Definition of done

See `Goal.md` — definition of done section. The concrete checks there are the acceptance criteria.

## Upstream reporting

While building on OKX.AI, x402, or OKX Payment SDK: if you hit a bug, doc gap, broken example, or unexpected behavior with a clean repro, capture it in `reports/` (what happened, minimal repro, environment, expected vs. actual). Draft the report but do not post it externally without following `plan.md`'s checkpoint rules.

## Hard rules

- **Out of scope**: See `Goal.md` — out of scope section
- **Mandatory skills**: `code-reviewer` and `simplify` run on every code change
- **Deadline**: Jul 27, 2026 23:59 UTC — no extensions
- **Sponsor tech is load-bearing**: x402 and X Layer payments are the core billing mechanism, not decoration
- **No decorative integrations**: If OKX.AI or x402 could be removed without breaking the journey, the design is wrong
- **README must pass deploy-to-github audit** (or hand-audit for: judge-runnable setup section, no AI slop, no assistant traces) before any push

## When in doubt

`Goal.md` wins over this prompt.
