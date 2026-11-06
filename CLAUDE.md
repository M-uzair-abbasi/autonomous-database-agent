# CLAUDE.md — Autonomous AI Database Agent

Read this before doing anything in this repo. It explains what we are building,
why, who owns which part, and the rules that must not be broken.

This file is the plan of record. The code and `docs/` were written before it and
still disagree with it in places (model serving, SQL validation, agent framework,
repo layout). Where they differ, this file wins and the code is what has to catch up.

---

## 1. What this project is

A Final Year Project at GIKI (Faculty of Computer Science and Engineering), CS-AI
department. Four students, three semesters, one year.

**Title:** Autonomous AI Database Agent: Integrating Self-Healing Infrastructure
and Conversational Business Intelligence

**One-sentence version:** One AI agent that lets non-technical people query a
PostgreSQL database in plain English, and at the same time watches that database's
health, diagnoses faults, and safely repairs the routine ones.

**Team**
| Member | Reg# | Owns |
|---|---|---|
| M. Uzair Abbasi | 2023547 | Orchestrator agent, NL2SQL, prompts, retrieval |
| Raneem Sultan | 2023597 | Validator, executor, schema analyzer, charts |
| Warisha Arshad | 2023757 | Telemetry, fault injection, anomaly detection |
| Yawar Kamal | 2023763 | Diagnosis, healing, approval flow, dashboard |

Supervisor: Mr. Ahsan Shah. Co-supervisor: Dr. Khurram Jadoon.

---

## 2. The research gap (why this project exists)

This is the thesis argument. Do not weaken it in code comments or docs.

Two research communities have developed separately:

- **Systems that diagnose but do not repair.** D-Bot (VLDB 2024) produces
  root-cause reports from database telemetry and stops there. Outage-Watch
  (ESEC/FSE 2023) predicts degradation and stops there.
- **Systems that act but do not diagnose.** OtterTune (SIGMOD 2017) tunes
  configuration knobs automatically but cannot reason about a novel fault or
  explain itself. The self-driving DBMS line (CIDR 2017) targets physical design
  and workload forecasting, not fault repair.

**DBA-Bench (arXiv:2607.22165, 2026) measured the gap between them.** Across 848
runs in live PostgreSQL environments: 277 runs diagnosed the root cause
correctly, and **172 of those (62%) then failed to complete the repair**. Safe
Pass across all systems was **12.4%**, against **93.4%** for a human DBA. The
paper names this the *diagnosis-to-remediation bottleneck*.

**Our contribution:** a verify-and-rollback remediation loop. We estimate the
effect of a fix before applying it, verify the target metric afterwards, and
reverse the change automatically when the metric does not recover — or, for an
action that has no reverse (`VACUUM` cannot be undone), escalate to a human
instead. No reviewed system does this.

**Secondary contribution:** putting conversational querying and database
operations behind one agent with shared memory and business context. Every
reviewed system does one half or the other, never both.

### What we do NOT claim
- A novel NL2SQL technique beating the state of the art.
- A new anomaly-detection algorithm.
- Fully autonomous unsupervised self-healing.
- A technical moat. The analytics/operations split is commercial, not technical.

---

## 3. Architecture

Five layers. Each talks only to the layer below it.

```
Chat Interface          Dashboard
       |                    ^
       v                    |
+--------------------------------------+        +-------+
|        Orchestrator Agent            | <----- |  LLM  |   (dashed = model dep.)
|   memory · context · tool routing    |        +-------+
+--------------------------------------+
                  |  tool dispatch
   +------+-------+--------+---------+-----------+
   v      v                v         v           v
NL2SQL  Validator       Charts   Anomaly     Self-Healing
   |      |                      Detection*      *
   v      v                           ^          |
Vector  PostgreSQL <------------------+----------+
Store
```
`*` = runs on a timer with no user in the conversation.

| Component | Owner | Job |
|---|---|---|
| Chat Interface | Yawar | React. Where questions go in. |
| Dashboard | Yawar | Health, anomalies, pending approvals, audit log. |
| Orchestrator Agent | Uzair | LangGraph state machine. ReAct loop, memory, business context, tool routing. |
| LLM | Uzair | Local open-weight SQL model via Ollama. |
| NL2SQL | Uzair | Schema-grounded prompt + retrieved examples → candidate SQL. |
| Vector Store | Uzair | pgvector. Schema chunks + verified question/SQL pairs. |
| Validator | Raneem | **Mandatory safety gate.** AST parse, read-only check, cost gate, row cap. |
| Query Executor | Raneem | Runs approved SQL under a read-only role. |
| Charts | Raneem | Result shape → declarative Plotly spec. |
| Schema Analyzer | Raneem | Catalog introspection → docs + index suggestions. |
| Anomaly Detection | Warisha | Isolation Forest + seasonal baseline over live metrics. |
| Self-Healing | Yawar | Diagnose → estimate → apply → verify → rollback. |
| PostgreSQL | shared | The database under management. |

---

## 4. The two flows

### Flow A — a user asks a question
1. Classify intent (data / visual / health / schema / open-ended BI).
2. Apply business context (resolve "last month", "revenue", "churned").
3. Retrieve relevant schema chunks and exemplar queries from pgvector.
4. Generate candidate SQL.
5. **Validator** — parse, confirm read-only, confirm objects exist, EXPLAIN cost
   check, apply row cap. On rejection, return the error to the model and retry,
   max 3 attempts.
6. Execute as the read-only role.
7. Build a chart spec if appropriate.
8. Agent decides whether it can answer. If not, loop (iteration cap applies).
9. Compose answer + chart + the SQL used. Write turn summary to memory.

A single question may legitimately fire several queries. "Why did revenue drop in
March?" needs the totals, then the breakdown by category. That loop is the point.

### Flow B — the autonomic loop (timer-driven, no user)
```
collect metrics -> score for anomaly -> diagnose root cause -> rank remedies
   -> policy check (tier?) -> [Tier 1] apply | [Tier 2] human approves | [Tier 3] alert only
   -> verify metric recovered? -> yes: log | no: ROLL BACK + escalate
```

---

## 5. SAFETY RULES — non-negotiable

These are the project's contribution. Never weaken them for convenience.

1. **The prompt is not a safety mechanism.** Never rely on instructing the model
   not to do something. Every guarantee lives below the prompt.
2. **Two database roles.** Analytical queries run as a `SELECT`-only role. Approved
   maintenance runs as a separate restricted role limited to allowlisted commands.
3. **Every generated statement is parsed with sqlglot** before execution. Any DDL
   or DML node, or a reference to a non-existent object, is rejected.
4. **Cost gate.** Run EXPLAIN and reject statements over the configured budget.
5. **Row cap and statement timeout** on every query.
6. **Record the undo before acting.** No remediation executes without its reverse
   operation already stored. For an action with no reverse (`ANALYZE`, `VACUUM`),
   the stored undo is an explicit *none — non-destructive*, with the reason.
7. **Verify after acting.** Re-measure the target metric after a settling period.
   If it has not recovered, issue the undo automatically; if the action has no
   undo, escalate to a human instead.
8. **Circuit breaker.** Two consecutive failed remediations disable auto-repair
   and require human re-enablement.
9. **Retrieved rows are untrusted input.** Data in the database may contain
   injected instructions. Never let retrieved content act as a prompt.

### Remediation tiers
| Tier | Actions | Authority |
|---|---|---|
| 1 — Automatic | `ANALYZE`, `VACUUM`, `CREATE INDEX CONCURRENTLY` (size-capped), terminate idle-in-transaction sessions, cancel queries past timeout, close excess idle connections | Applied automatically. None change committed data. Only `CREATE INDEX CONCURRENTLY` has a true undo (`DROP INDEX`); `ANALYZE` and `VACUUM` cannot be undone but destroy nothing; ending or cancelling a session discards that client's uncommitted work. |
| 2 — Approval | `REINDEX CONCURRENTLY`, drop unused index, config changes (`work_mem`, per-table autovacuum), cancel an *active* long query, heavy bloat reclamation | Proposed with evidence + estimated impact. Executes only after a named person approves. |
| 3 — Advisory | Disk exhaustion, crash, corruption, accidental mass deletion, hardware failure, suspected security incident | Diagnose and escalate. **Never act autonomously.** |

`VACUUM FULL` is never automatic — it locks the whole table.

---

## 6. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | — |
| Database | PostgreSQL 16 | `pg_stat_statements`, `EXPLAIN`, rich catalog views |
| Vector store | pgvector | Lives inside the same Postgres; one fewer service |
| Agent | LangGraph | Explicit state graph: loops, branching, persisted state, and `interrupt()` for the human approval step |
| LLM plumbing | LangChain | Model connectors, prompt templates, retrievers, tool binding |
| Model | Open-weight SQL-specialised model via Ollama | Local = zero per-query cost + data residency |
| SQL validation | **sqlglot** | Parses SQL to an AST so statement type is verified programmatically, not by regex |
| Anomaly ML | scikit-learn (Isolation Forest), statsmodels (seasonal decomposition) | Interpretable; trains on limited telemetry |
| Fine-tuning (optional) | Hugging Face PEFT + bitsandbytes + TRL | QLoRA so a 7B model fits one consumer GPU |
| Charts | Plotly | Spec is JSON — validate before rendering |
| Backend | FastAPI + Pydantic | Async, typed, auto docs |
| Frontend | Streamlit (Terms I–II) → React (Term III) | Ship fast, polish later |
| Packaging | Docker Compose | One command brings up the demo |

**Do not** use raw string matching to validate SQL. **Do not** use TensorFlow for
fine-tuning (that is PyTorch/HF); TensorFlow appears only in an optional LSTM
autoencoder comparison against Isolation Forest.

---

## 7. Repo layout and the contracts

```
fyp/
├── docker-compose.yml      PostgreSQL 16 + pgvector
├── contracts/              THE FOUR SHARED TYPES — see below
├── seed/                   sample business schema + generated data
├── graph/                  LangGraph skeleton, one node per module
├── modules/
│   ├── ask/        (Uzair)
│   ├── execute/    (Raneem)
│   ├── watch/      (Warisha)
│   └── fix/        (Yawar)
├── api/                    FastAPI
└── tests/
```

**The contracts are frozen.** Four Pydantic models define every boundary between
owners. Changing one means two people rewrite code, so a change requires all four
members to agree in the group chat first.

```python
SQLCandidate      # ask     -> execute
  {question, sql, tables_used, confidence}

ExecutionResult   # execute -> ask
  {rows, columns, chart_spec} | {rejected, reason}

AnomalyEvent      # watch   -> fix
  {timestamp, severity, metrics, deviating_features}

RemediationRecord # fix     -> dashboard
  {diagnosis, action, predicted, actual, verified, undo}
```

Each person works against **stubs** of their neighbours' modules until the real
thing exists. Never edit another owner's folder. Never edit `graph/` except your
own node.

---

## 8. Datasets

| Dataset | Tests | Source |
|---|---|---|
| **BIRD** (NeurIPS 2023) | NL2SQL execution accuracy — primary benchmark | Download. Use **BIRD Mini-Dev** for the PostgreSQL-compatible subset; the original ships as SQLite |
| **Spider** (EMNLP 2018) | Cross-domain generalisation | Download |
| **nvBench / VisEval** | Chart validity, legality, readability | Download |
| Project domain set | Accuracy on a realistic business schema | We write and review it |
| **Injected fault set** | Anomaly detection + healing | We generate it |
| Adversarial prompt set | Validator refusal rate | We write it |

### Fault injection
No public dataset of labelled PostgreSQL faults exists at this scale, so we cause
our own, in a disposable Docker container, never on shared or real data. Run
`pgbench` as background load, then inject: dropped index, lock contention from an
idle-in-transaction session, connection exhaustion, stale statistics, accumulated
dead rows, and (for alert-only testing) disk pressure. Log fault start/end times —
that is the ground truth label.

---

## 9. Evaluation metrics

Decided in Term I because they determine what the system must expose.

| Metric | Definition | Measured against |
|---|---|---|
| Execution accuracy | Does generated SQL return the correct rows | BIRD, Spider, domain set |
| Detection precision/recall | Real faults caught without false alarms | Injected fault set |
| **Safe Pass** | Fix both resolved the fault *and* caused no harm | Injected faults, Tier 1 — use the DBA-Bench definition |
| Refusal rate | Unsafe statements blocked before execution | Adversarial prompt set |
| Chart validity | Valid, legal, readable | VisEval rubric |
| Latency & cost | p50/p95 and cost per query | Local vs hosted model |

**Report benchmark numbers alongside a manually reviewed figure.** Published work
documents annotation errors and overly strict result comparison in these
benchmarks; a bare leaderboard number is not a sufficient account of quality.

Do not compare our Safe Pass directly against DBA-Bench's 12.4%. Their benchmark
is open-ended; ours is five known fault types with pre-written fixes. Easier
problem, so a higher score proves nothing on its own.

---

## 10. Term plan

| Term | Deliverable |
|---|---|
| **I (6th sem)** | Literature review; PostgreSQL + telemetry + fault-injection harness running; retrieval pipeline; NL2SQL prototype with validator; **a measured baseline accuracy number** |
| **II (7th sem)** | Orchestrator with memory and business context; chart generation; anomaly detector trained on injected faults; first integrated demo |
| **III (8th sem)** | Supervised healing with tiers, impact estimation, verification, rollback; schema optimiser; full benchmark suite; local-vs-hosted comparison; thesis |

Term I's deliverable is a **number**, not a demo. Everything afterwards is
measured against it.

---

## 11. Conventions for working here

- **Read this file before writing code.** If a change contradicts Section 5, stop
  and raise it rather than implementing it.
- **Stay in your own module.** Cross-module changes go through a contract
  discussion first.
- **Never commit credentials.** Connection strings come from environment variables.
- **Never point anything at a non-disposable database.** Fault injection destroys
  data by design.
- **Every remediation path needs a test that proves rollback works**, not just a
  test that proves the fix works.
- **Write the undo before the do.** If you cannot express the reverse operation,
  the action must be shown to destroy nothing (and the record must say why), or
  it does not belong in Tier 1.
- **Log the agent's reasoning.** Every THOUGHT / ACTION / OBSERVE step is logged —
  it is the demo, and it is the debugging surface.
- CI runs the full pipeline on every push. A contract break fails the build; that
  is deliberate and replaces a weekly sync meeting.

---

## 12. Key references

- Li, J. et al. (2023). *Can LLM Already Serve as A Database Interface? A Big Bench
  for Large-Scale Database Grounded Text-to-SQLs* (BIRD). NeurIPS.
- Yu, T. et al. (2018). *Spider.* EMNLP, 3911–3921.
- Yao, S. et al. (2023). *ReAct: Synergizing Reasoning and Acting in Language
  Models.* ICLR.
- Zhou, X. et al. (2024). *D-Bot: Database Diagnosis System using Large Language
  Models.* PVLDB 17(10), 2514–2527.
- Van Aken, D. et al. (2017). *Automatic DBMS Tuning Through Large-scale Machine
  Learning* (OtterTune). SIGMOD.
- Pavlo, A. et al. (2017). *Self-Driving Database Management Systems.* CIDR.
- Chen, J. et al. (2026). *DBA-Bench: A Production-Fidelity Benchmark for
  LLM-Based Database Operations Agents.* arXiv:2607.22165.
- Agarwal, S. et al. (2023). *Outage-Watch: Early Prediction of Outages using
  Extreme Event Regularizer.* ESEC/FSE.
- Hu, E. J. et al. (2022). *LoRA.* ICLR. / Dettmers, T. et al. (2023). *QLoRA.* NeurIPS.
- Liu, F. T. et al. (2008). *Isolation Forest.* ICDM.
