# TidyData - clean any messy spreadsheet, inside Anna

**AI proposes - the engine proves - you approve.**

TidyData is an Anna App for the chore everyone does and nobody trusts: cleaning a
messy spreadsheet. Paste a CSV, and Anna proposes plain-English fixes (trim
spaces, standardize dates, dedupe rows, normalize currency, split columns). A
**deterministic Executa engine - never the model - applies each fix and reports
the exact rows and cells it changed.** Nothing touches your data until you click
**Approve**.

> The model plans; the engine is the only thing that ever reads, transforms, or
> counts your data. So "47 cells changed" is a measured fact, not a hallucination.

---

## Why it fits the hackathon

| Judging criterion        | How TidyData delivers                                                                 |
| ------------------------ | ------------------------------------------------------------------------------------- |
| Usefulness & user value  | Spreadsheet cleaning is a universal weekly chore across every profession.              |
| Working demo             | Runs fully offline via `anna-app dev --no-llm` - engine is real, proposals fall back to a deterministic baseline. |
| Meaningful use of AI     | The LLM authors a cleaning *plan* in plain English; every step is engine-verified.    |
| Fit with Anna            | Uses **both** Executa flavors - a Tool (`tidy-engine`) and a Skill (`tidy-coach`) - plus host LLM, storage, chat, and window APIs. |
| Creativity & execution   | A structurally-unbypassable human-review gate: the agent has no path to mutate data except through approved, engine-applied ops. |

## The core guarantee (the architecture insight)

The host LLM can only **propose** operations from a fixed vocabulary. The bundle
validates each proposal against that vocabulary, asks the engine to **preview**
its real effect, and applies it **only on explicit human approval**. The engine
owns the table and every count. There is no code path where the model edits your
data or invents a statistic - making the review gate unbypassable by design.

## What's inside

```
anna-app-tidy-data/
├── manifest.json                 # schema-2 app: permissions, host_api ACL, prompt addendum
├── app.json                      # store listing + bundled_executas map
├── bundle/                       # static-spa UI (no build step, no CDNs)
│   ├── index.html  style.css  app.js  icon.svg
├── executas/
│   ├── tidy-engine-python/       # Executa Tool - deterministic source of truth
│   │   ├── tidy_engine_plugin.py
│   │   ├── test_engine_contract.py   # 15 stdlib contract tests
│   │   ├── executa.json  pyproject.toml  package_binary.sh
│   └── tidy-coach/               # Executa Skill - propose-then-ratify protocol
│       └── SKILL.md  executa.json
├── fixtures/happy-path.jsonl
└── .github/workflows/build-tidy-engine-binary.yml
```

## Run locally

```bash
cd anna-app-tidy-data
npm install
npx anna-app validate --strict
npx anna-app dev            # or: npx anna-app dev --no-llm  (fully offline)
```

Then in Anna chat, `#tidy-data` (or open the window) → paste rows / **Load
sample** → **Analyze** → Approve the fixes you want → **Export clean CSV**.

Test the engine directly (no Anna, stdlib only):

```bash
python executas/tidy-engine-python/test_engine_contract.py    # 15 checks
echo '{"jsonrpc":"2.0","method":"describe","id":1}' | python executas/tidy-engine-python/tidy_engine_plugin.py
```

## Cleaning operations

`trim_whitespace` | `drop_empty_rows` | `drop_empty_columns` | `dedupe_rows` |
`normalize_case{column,mode}` | `standardize_dates{column}` |
`normalize_numbers{column}` | `fill_blanks{column,value}` |
`split_column{column,delimiter,into[]}` | `rename_column{column,to}`

## Deployment

1. The GitHub Action (`build-tidy-engine-binary.yml`) automatically builds and publishes platform-specific binaries (macOS ARM64, macOS x86_64, Linux x86_64) to GitHub Releases.
2. On the Anna platform (https://anna.partners), set the Tool's Distribution Type to **Binary** and paste the three GitHub Release URLs from the latest build.
3. Click **Install Essentials** to complete deployment.

Built for the **Anna AI-Native App Hackathon**.
