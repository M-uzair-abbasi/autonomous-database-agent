# PROJECT_STATE.md — read this before writing any file

Two AI agents (Claude Code and Codex/GPT) share this working directory.
`CLAUDE.md` and `AGENTS.md` are the same original project brief. **They describe
the plan, not reality.** This file records what is actually true right now.

Last verified: 2026-08-12.

---

## 1. Correction to the brief — important

Section 3 of `CLAUDE.md` / `AGENTS.md` claims a working scaffold "already
exists (built and tested)", listing `schema_context.py`, `model_client.py`,
`safety.py`, `nl_to_sql.py`, `app/main.py`, `eval/eval_harness.py`,
`eval/test_set.json`, and `requirements.txt`.

**Those files do not exist.** Verified by directory scan of this machine and of
`$HOME`. There is no `scm_agent` scaffold anywhere on this system, and this
directory is not a git repository with history to recover from. Treat section 3
as the intended design, not as a record of completed work.

Do not import from, or assume the behaviour of, any file listed in section 3
until you have confirmed it exists on disk.

## 2. What actually exists right now

| Path | Status |
|---|---|
| `scm_agent/pipeline/config.py` | **Built and tested.** Model swap point + dialect registry. Self-test passes. |
| `scm_agent/db/schema.sql` | **Built, not yet executed.** Sample e-commerce schema + synthetic seed data. |
| `docker-compose.yml` | **Built, not yet started.** Dedicated Postgres for this project on host port **5434**. |
| `scm_agent/{__init__,pipeline/__init__,app/__init__,eval/__init__}.py` | Empty package markers. |
| Everything else in section 3 | **Does not exist.** |

## 3. Decisions made after the brief was written

These supersede or extend `CLAUDE.md` / `AGENTS.md`. Do not relitigate without
asking the user.

1. **Multi-dialect SQL is in scope.** The NL→SQL path targets PostgreSQL,
   MySQL, SQL Server and SQLite — not Postgres alone. The user chose this
   explicitly over a Postgres-only build.
2. **NoSQL is out of scope.** MongoDB / Neo4j / Redis are excluded. Different
   query languages, different safety model, no equivalent healing actions.
3. **The self-healing engine stays PostgreSQL-only.** `VACUUM`, `REINDEX` and
   `pg_stat_*` have no portable cross-engine equivalent. Pillar 2 does not
   consult the dialect registry.
4. **Base OmniSQL is SQLite-trained.** Confirmed from the model card: SynSQL-2.5M
   was synthesised in the SQLite dialect, and the stock prompt hardcodes
   `Database engine: SQLite`. Expect base-model dialect errors on Postgres
   (`strftime`, casts, `ILIKE`, `RETURNING`). Closing that gap is the point of
   the fine-tune, and the baseline number is a headline result for the report —
   measure base-model accuracy **before** fine-tuning.
5. **Model source:** `seeklhy/OmniSQL-7B` on Hugging Face, Apache 2.0, not
   gated. 14B/32B exist at the same namespace. A LoRA adapter is tied to the
   base model size — a 7B adapter will not load on 14B.

## 4. File ownership — avoid clobbering each other

This directory has **no version control**, so an overwrite is unrecoverable.
Stay inside your lane unless the user reassigns it.

| Owner | Paths |
|---|---|
| **Claude Code** | `scm_agent/pipeline/**`, `scm_agent/eval/**`, `scm_agent/db/**` |
| **Codex / GPT** | `scm_agent/healing/**` (Pillar 2: monitoring, anomaly rules, corrective actions, audit log) |
| **Announce before editing** | `app/main.py`, `docker-compose.yml`, `requirements.txt`, `CLAUDE.md`, `AGENTS.md`, this file |

Pillar 1 (NL→SQL) and Pillar 2 (self-healing) share no source files, so both can
proceed in parallel with zero conflicts. `app/main.py` is the only integration
point — whoever touches it should say so first.

If you must edit a file you do not own: tell the user, do not silently rewrite.

## 5. Invariants — do not break these

1. **`pipeline/config.py` is the single model swap point.** One env var moves
   the system mock → base OmniSQL on vLLM → fine-tuned LoRA. Nothing else may
   hardcode a model name, base URL or provider. `CLAUDE.md` §6 protects this
   pattern explicitly.
2. **All dialect facts live in `DIALECT_SPECS` in `config.py`.** Do not scatter
   dialect string literals across modules.
3. **`/health` must never leak the DSN.** It carries a password. `summary()` is
   already tested for this.
4. **The NL query path is read-only.** Safety enforcement must be dialect-aware
   — MySQL treats `#` as a line comment, which is a real injection surface the
   other dialects do not have.
5. **Prompt format is a shared contract.** The exact prompt built for inference
   must match the format the training data is generated in. If these diverge,
   the fine-tune silently underperforms (train/serve skew). Nobody changes the
   prompt builder unilaterally once teammates start generating data.

## 6. Environment facts (this machine)

- **No native PostgreSQL.** No `psql`, no `pg_isready`, no `postgresql` service.
- **Host port 5432 is occupied** by an unrelated project's Postgres container.
  Port 5433 is claimed by the `burk-scm` project. **This project uses 5434.**
- **Docker requires `sudo`** — the user is not in the `docker` group.
- **`pip install` is blocked** by PEP 668 (Ubuntu 24.04, externally managed).
  `python3.12-venv` is **not installed**, so `python3 -m venv` produces a broken
  venv with no pip. Do **not** work around this with
  `--break-system-packages`; it risks the system Python. The fix is
  `sudo apt install -y python3.12-venv`, which needs the user's password.
- **`sqlite3` works** (stdlib, v3.45.1) — usable for testing with zero installs.
- Python 3.12.3.

## 7. Open blockers (need the user)

1. `sudo apt install -y python3.12-venv` — required before any Postgres driver.
2. `sudo docker compose up -d` — starts this project's database on 5434.
3. **`git init` is strongly recommended** before both agents write in parallel.
   Without it there is no recovery from an overwrite.
