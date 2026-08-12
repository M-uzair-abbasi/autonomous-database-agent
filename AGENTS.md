# Project Brief: SCM-OVS — Autonomous AI Database Agent

Paste this whole file as your first message to Codex, or save it as
`AGENTS.md` in your project root (Codex reads that automatically).
It gives a fresh session full context so you don't have to re-explain
everything each time.

---

## 1. What this project is

**Academic context:** Final Year Project, BS Artificial Intelligence, GIKI
(Ghulam Ishaq Khan Institute), Pakistan. Team of 4. Supervisor: Mr. Ahsan
Shah. Co-supervisor: Dr. Khurram Jadoon. Scoped across 3 semesters.

**What we're building:** "Autonomous AI Database Agent" — a platform that
lets non-technical users query a PostgreSQL database in plain English, while
a background engine monitors the database and autonomously fixes common
health problems (bloat, idle connections, fragmented indexes) without a DBA.

**Full objectives (from the approved scope doc):**
- BO-1: NL-to-SQL translation using a schema-aware LLM
- BO-2/3: Self-healing engine — monitors DB health, applies safe corrective actions (VACUUM, REINDEX, connection cleanup), rule-based anomaly detection
- BO-4: Conversational BI — natural-language explanations + auto-generated charts alongside query results
- BO-5/9: Schema documentation + optimization suggestions (LOWER PRIORITY — stretch goals)
- BO-6: Plug-and-play — connects to any Postgres DB via credentials, no schema changes
- BO-7/8: Safety — all healing actions logged, reversible, read-only enforcement on the NL query path; full audit trail

## 2. Strategic decisions already made (do not relitigate these without asking)

- **Two flagship pillars, everything else is a stretch goal:**
  1. **Fine-tuned NL-to-SQL** — base model is **OmniSQL-7B or 14B** (2025 model, strong Spider benchmark, trained on synthetic data across 16k+ schemas). NOT SQLCoder (dated, 2023). NOT training from scratch.
  2. **Self-healing database engine** — the actual differentiator vs. existing tools (pganalyze, Metabase, D-Bot all lack this combination per our lit review).
- **Only the NL-to-SQL component gets fine-tuned.** Conversational BI explanations and schema docs stay on API calls (GPT-5/Codex) — this was the original scope doc's feasibility call, we're keeping it.
- **Compute is not yet finalized.** Plan, in order of preference: (1) ask GIKI HPC/AI lab for GPU allocation, (2) Google Colab Pro, (3) cloud rental (RunPod/Vast.ai) for a single 16-24GB GPU. We do NOT need multi-GPU infrastructure — LoRA/QLoRA fine-tuning on a 7-14B model fits on one consumer/cloud GPU.
- **Fine-tuning method: LoRA/QLoRA**, using **LLaMA-Factory** or **Axolotl** — not custom training infra.
- **Model serving: vLLM**, because it exposes an OpenAI-compatible API endpoint AND natively supports loading LoRA adapters alongside the base model — this is what makes the "build now, fine-tune later" swap painless.

## 3. What already exists (built and tested)

A working pipeline scaffold already exists with the model layer deliberately
made swappable, so the rest of the system can be built and tested before
the fine-tuned model is ready. Confirmed working end-to-end:

```
scm_agent/
├── pipeline/
│   ├── config.py          # THE swap point — one env var changes mock -> vLLM base model -> fine-tuned LoRA
│   ├── schema_context.py  # introspects Postgres (tables, columns, foreign keys) -> text for prompts
│   ├── model_client.py    # mock provider (no GPU needed) OR openai_compatible provider (vLLM later)
│   ├── safety.py          # read-only enforcement — blocks INSERT/UPDATE/DELETE/DROP/ALTER/etc.
│   └── nl_to_sql.py       # orchestration: question -> schema-aware prompt -> SQL -> safety check -> execution
├── app/
│   └── main.py             # FastAPI: /health, /schema, /query endpoints
├── eval/
│   ├── test_set.json       # question -> expected SQL pairs
│   └── eval_harness.py     # computes execution accuracy — THIS produces the base-vs-fine-tuned-vs-GPT5 comparison table for the report
├── db/
│   └── schema.sql           # sample e-commerce schema (customers/orders/products/order_items) for local testing
└── requirements.txt
```

Tested and confirmed working: schema introspection against live Postgres,
full question→SQL→execution→results flow on the mock model, safety layer
correctly blocking destructive queries, FastAPI endpoints responding
correctly, eval harness producing a real accuracy score (5/5 on mock).

**Not yet done:** real OmniSQL hasn't been loaded or fine-tuned anywhere yet
— that's blocked on compute access. Everything above was built and proven
using a mock model specifically so this work didn't have to wait.

## 4. Full roadmap (phase order)

1. **Fine-tuning pipeline** (Pillar 1): design 2-3 real target schemas → generate synthetic question/SQL training data via GPT-5/Codex → **manually verify/correct it** (this is the most underestimated, highest-risk step — budget real time) → LoRA fine-tune OmniSQL via LLaMA-Factory/Axolotl → evaluate execution accuracy with a held-out test set → error-analyze (multi-table JOINs and nested subqueries are typically hardest) → iterate.
2. **Self-healing engine** (Pillar 2, can run in parallel with #1): poll `pg_stat_activity` / `pg_stat_user_tables` / `pg_stat_user_indexes` → rule-based anomaly thresholds → corrective actions (VACUUM/REINDEX/connection termination) → log every action with timestamp + pre-action snapshot for rollback → stress-test with deliberately induced anomalies so it can be demoed live.
3. **System integration:** FastAPI backend (exists) + React dashboard (query interface, live health view, audit trail view) + app DB storing query history/anomaly events/healing logs.
4. **Conversational BI layer:** pass SQL results to GPT-5/Codex API for NL explanation (not fine-tuned) + rule-based chart type selection based on result shape.
5. **Stretch goals only if time remains:** schema documentation generator, schema optimization suggestions.
6. **Final phase:** security testing (SQL injection attempts through the NL layer, confirm read-only enforcement holds), edge-case testing, rehearsed live demo script for the viva.

## 5. Team division

- **Person A (ML/fine-tuning):** synthetic data generation + verification, LoRA fine-tuning, evaluation benchmarking.
- **Person B (Backend/self-healing):** monitoring module, anomaly rules, corrective actions, audit logging.
- **Person C (Frontend/integration):** React dashboard, wiring everything together into one working app.
- **Person D (BI/QA/writing):** conversational BI API calls, chart logic, security/edge-case testing, report + viva script — runs continuously, not just at the end.

## 6. How I want to work with Codex on this

- **Go step by step.** Don't generate the whole remaining project in one shot — build one piece, show me it actually running/tested, then move to the next.
- **Explain what you're doing as you go**, not just the output — I'm a student and want to actually understand this, not just receive a black box.
- **Always test before declaring something done** — run it, show me real output, not just written code.
- **Don't change the swappable model-config pattern in `pipeline/config.py`** without flagging it — that pattern is deliberate and the whole later fine-tuning swap depends on it staying clean.
- **Flag it clearly if something I'm asking for conflicts with the approved scope doc or risks the timeline** — I'd rather know early than find out at the viva.

## 7. Immediate next step

Right now: continue from the existing scaffold. Next concrete task is
[FILL IN: e.g. "start the self-healing engine's monitoring module" or
"build the synthetic training data generation script" — tell Codex
which one you want to start with].
