# SCM-OVS — Project brief

**Autonomous AI Database Agent**

Final Year Project, BS Artificial Intelligence, Ghulam Ishaq Khan Institute.
Team of 4, scoped across 3 semesters.
Supervisor: Mr. Ahsan Shah. Co-supervisor: Dr. Khurram Jadoon.

---

## 1. What this is

A platform that lets non-technical users query a PostgreSQL database in plain
English, while a background engine monitors the database and autonomously fixes
common health problems — table bloat, idle connections, degraded indexes —
without a DBA.

### Objectives

| ID | Objective |
|---|---|
| BO-1 | Natural-language to SQL translation using a schema-aware LLM |
| BO-2/3 | Self-healing engine: monitor health, apply safe corrective actions (VACUUM, REINDEX, connection cleanup), rule-based anomaly detection |
| BO-4 | Conversational BI: natural-language explanations and generated charts alongside query results |
| BO-5/9 | Schema documentation and optimisation suggestions (lower priority — stretch goals) |
| BO-6 | Plug-and-play: connect to any PostgreSQL database via credentials, with no schema changes |
| BO-7/8 | Safety: all healing actions logged and auditable, read-only enforcement on the query path, full audit trail |

**Note on BO-7/8 wording.** Earlier drafts described healing actions as
"reversible". They are not, and the claim should not be repeated. `VACUUM`
cannot be undone, and a terminated backend cannot be restored. What the system
provides is *auditable and non-destructive*: a before/after snapshot, a complete
action record, and a dry-run mode. This distinction is defensible; "reversible"
is not.

## 2. Decisions

Two flagship deliverables. Everything else is a stretch goal.

1. **Fine-tuned natural-language-to-SQL.** Base model `seeklhy/OmniSQL-7B`
   (Apache 2.0). Not SQLCoder — dated. Not trained from scratch.
2. **Self-healing database engine.** The actual differentiator: pganalyze,
   Metabase and D-Bot each lack this combination per the literature review.

- **Only the NL-to-SQL component is fine-tuned.** Conversational BI explanations
  and schema documentation remain API calls.
- **Fine-tuning method: LoRA/QLoRA**, via LLaMA-Factory or Axolotl — not custom
  training infrastructure. A 7B model fits on a single 16–24 GB GPU.
- **Serving: vLLM**, because it exposes an OpenAI-compatible endpoint and loads
  LoRA adapters alongside the base model, which is what makes the
  build-now/fine-tune-later swap a configuration change.
- **Multi-dialect querying.** PostgreSQL, MySQL, SQL Server and SQLite are in
  scope for the query path. NoSQL engines are explicitly out of scope: different
  query languages, different safety model, and no equivalent healing actions.
- **Self-healing stays PostgreSQL-only.** `VACUUM`, `REINDEX` and `pg_stat_*`
  have no portable cross-engine equivalent.
- **Compute not yet secured.** Order of preference: GIKI HPC/AI lab allocation,
  then Colab Pro, then short-term cloud rental of a single GPU.

### A known gap worth stating plainly

The base model was trained on SQLite-dialect data, and its stock prompt hardcodes
`Database Engine: SQLite`. Base-model dialect errors on PostgreSQL are therefore
expected — `strftime`, casts, `ILIKE`, `RETURNING`. Closing that gap is the
purpose of the fine-tune, which makes the base-model baseline a headline result
rather than an embarrassment. **Measure base-model accuracy before fine-tuning.**

A LoRA adapter is tied to the base model it was trained on: a 7B adapter will not
load onto a 14B model. The training data transfers; the adapter does not.

## 3. Roadmap

1. **Fine-tuning pipeline.** Design target schemas → generate synthetic
   question/SQL training data → **manually verify and correct it** (the most
   underestimated, highest-risk step) → LoRA fine-tune → evaluate execution
   accuracy on a held-out set → error-analyse (multi-table joins and nested
   subqueries are typically hardest) → iterate.
   Generate training data in **PostgreSQL** dialect and across **many** schemas,
   not two or three. Fine-tuning on a narrow set trades away the cross-domain
   generalisation the base model already has.
2. **Self-healing engine** (parallel to 1). Poll `pg_stat_activity`,
   `pg_stat_user_tables`, `pg_stat_user_indexes` → rule-based thresholds →
   corrective actions → journal every action with a pre-action snapshot →
   stress-test with deliberately induced anomalies so it can be demonstrated
   live.
3. **Integration.** FastAPI backend, React dashboard (query interface, live
   health view, audit trail), application database for query history and events.
4. **Conversational BI.** Pass results to an API model for explanation, plus
   rule-based chart selection driven by result shape.
5. **Stretch goals if time remains.** Schema documentation generator, schema
   optimisation suggestions.
6. **Final phase.** Security testing (injection attempts through the natural
   language path, read-only enforcement), edge-case testing, rehearsed
   demonstration script.

## 4. Team

| Role | Responsibility |
|---|---|
| ML / fine-tuning | Synthetic data generation and verification, LoRA fine-tuning, evaluation benchmarking |
| Backend / self-healing | Monitoring, anomaly rules, corrective actions, audit logging |
| Frontend / integration | React dashboard, wiring the system together |
| BI / QA / writing | Conversational BI, chart logic, security and edge-case testing, report and demonstration script — continuous, not end-loaded |

## 5. Working conventions

- Build one piece at a time and demonstrate it running before moving on.
- Test before declaring anything done; show real output, not just code.
- `pipeline/config.py` is the single model swap point. Do not let a model name,
  base URL or provider be hardcoded anywhere else — the entire fine-tuning
  handover depends on that pattern staying clean.
- The inference prompt format is a contract shared with training-data
  generation. Once data generation begins, nobody changes it unilaterally.
- Flag anything that conflicts with the approved scope or threatens the timeline
  early, not at the end.

See `docs/ARCHITECTURE.md` for the system design and `docs/TESTING.md` for how
to run the suites and what has and has not been verified.
