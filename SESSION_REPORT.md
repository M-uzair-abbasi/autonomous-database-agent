# What was built — session report, 2026-08-12

Written for the project owner to review. Plain language, no jargon where it can
be avoided. Claude Code built Pillar 1 (natural language → SQL); Codex/GPT built
Pillar 2 (self-healing) in parallel in its own directory.

---

## 1. The short version

**Pillar 1 is complete as a working scaffold and every part of it runs.**

You can start the server right now and ask it questions in English about a
database, and it will answer them with real data. The AI model is still a fake
("mock") stand-in, because there is no GPU yet — but every other piece is real,
and swapping the fake model for the real OmniSQL is one environment variable.

Seven modules, 5,993 lines. All seven self-tests pass. The whole thing was also
verified over real HTTP, not just in unit tests.

---

## 2. What exists now

| File | What it does | Verified? |
|---|---|---|
| `pipeline/config.py` | The swap point: one env var moves mock → base OmniSQL → fine-tuned. Also holds the dialect registry. | Yes |
| `pipeline/schema_context.py` | Looks inside any database and writes out its structure for the AI. | Yes, SQLite only |
| `pipeline/safety.py` | Blocks anything that isn't a read. The security layer. | Yes, 42 + 12 cases |
| `pipeline/model_client.py` | Talks to the model — fake one now, vLLM later. | Yes |
| `pipeline/nl_to_sql.py` | Ties it together: question → prompt → model → SQL → safety → run. | Yes |
| `app/main.py` | Web API: `/health`, `/schema`, `/query`. | Yes, live HTTP |
| `eval/eval_harness.py` | Measures accuracy. Produces the report's headline table. | Yes |
| `eval/test_set.json` | 15 graded test questions (5 easy, 6 medium, 4 hard). | Yes |
| `db/schema.sql` | Sample shop database with fake data. | Yes |
| `docker-compose.yml` | Your PostgreSQL, port 5434. | **NOT started** |

---

## 3. Each piece, and why it matters

### `config.py` — the swap point
One environment variable decides whether the system talks to a fake model, to
real OmniSQL on a GPU, or to your fine-tuned version. Nothing else in the code
knows or cares.

**Why it matters:** it is the reason you could build the entire system before
getting a GPU. On the day compute arrives, you change a variable — not code.
`/health` also reports which stage is active, so at the viva you cannot
accidentally demo the fake model and claim it is the fine-tuned one.

### `schema_context.py` — reading any database
Connects to a database it has never seen, finds every table, column, primary key
and foreign key, and writes it out as `CREATE TABLE` statements for the AI.

**Why it matters:** this is objective BO-6, "plug and play". The database is
never baked into the model — it is re-read on every single question. That is
what makes it work on a database you have never shown it.

It can also include a few real example values as comments, like
`status VARCHAR(20), -- e.g. delivered, shipped, pending`. That one line stops
the model guessing `'Delivered'` and returning zero rows — perfect-looking SQL
that silently answers nothing.

### `safety.py` — the read-only wall
Every query the AI produces is checked before it runs. Only `SELECT` and
`WITH … SELECT` get through.

**Why it matters:** this is the only thing between "a user typed a sentence" and
"arbitrary SQL ran on your database". Objectives BO-7/8, and your final project
phase explicitly tests it with injection attempts.

It is dialect-aware because you chose multi-dialect: MySQL treats `#` as a
comment and the others do not, which is a real hiding place for an attack.

### `model_client.py` — the model layer
The fake model deliberately imitates the real one: it writes reasoning prose
first, then the SQL in a fenced code block, exactly like real OmniSQL does.

**Why it matters:** if the fake returned clean bare SQL, the parser would be
tested against something unrealistic and would break the first time a real GPU
was attached. This is the most common way a "build now, GPU later" plan fails.

### `nl_to_sql.py` — the orchestrator
Question → prompt → model → extract SQL → **safety check** → run → results.

**Why it matters:** the order is security-critical, and it executes the exact
string the safety layer approved, not the raw model output. Checking one string
and running a different one is precisely how these layers get bypassed.

Its prompt is byte-identical to OmniSQL's published template, with only the
`Database Engine:` line parameterised for your multi-dialect decision.

### `app/main.py` — the web API
Three endpoints. All the logic sits in plain functions; the web framework is a
thin wrapper.

**Why it matters:** it means the API can be tested without starting a server,
and Person C can build the React dashboard against it.

### `eval_harness.py` + `test_set.json` — the measurement
Runs the model's SQL and the correct SQL against the same database and compares
the **results**, not the text. Two different-looking queries that return the same
answer are both correct.

**Why it matters:** this produces the base-vs-fine-tuned-vs-GPT-5 table that is
your report's main result. It also breaks failures down by reason — parse
failure, safety denial, execution error, wrong answer — which is your error
analysis section.

Current honest baseline: **7.1%** on the fake model. That number is *supposed*
to be low. Nothing was tuned to make it look better.

---

## 4. Five real problems found and fixed

These are the parts worth reading, because they were genuine defects, not
cosmetics.

**1. A SQL injection bypass.** The first version of the safety layer could be
defeated with `SELECT $$'$$; DROP TABLE t; SELECT $$'$$`. PostgreSQL treats the
apostrophe inside `$$…$$` as ordinary data, but the checker read it as the start
of a string and blanked out everything to the next apostrophe — swallowing the
`DROP` in between. All three statements are valid PostgreSQL, so it would have
executed. Found by an adversarial pass, not by the original tests, which all
passed. Fixed and locked with regression cases.

**2. The test set could not detect a LEFT JOIN error.** The sample data had no
customer without orders and no product never sold, so `LEFT JOIN` and
`INNER JOIN` returned identical results. A model making that mistake would have
scored *correct*. Fixed by adding one customer with no orders and one unsold
product — the two joins now return 13 and 12 rows.

**3. The evaluation could have understated accuracy.** One test case uses
PostgreSQL-only date syntax that SQLite rejects. If the harness counted the
*reference* query failing as a *model* failure, your headline number would be
wrong. It is now bucketed separately as "skipped", excluded from the denominator,
and stated openly in the output.

**4. `/health` could have leaked the database password.** Tested explicitly by
planting a password and confirming it appears nowhere in the response.

**5. I was wrong about the environment.** I told you no Python packages were
installable and wrote that into the shared state file. FastAPI, uvicorn and
pydantic were in fact already installed — I had checked `psycopg`, found it
missing, and generalised. Corrected. The practical upshot is good news: the
API server runs today.

---

---

## 4b. The independent adversarial review — and three more bugs it found

After everything was built, a separate agent was told to attack it and to assume
the self-written tests proved nothing. That assumption was correct.

**What it could NOT break (worth putting in your report):**

- **73 forged malicious model replies. Zero database changes.** Every table
  survived, every row count unchanged, across all four dialects. Payloads
  included stacked statements, data-modifying CTEs, `SELECT … INTO OUTFILE`,
  the dollar-quote desync, writes hidden behind `--`, behind MySQL `#`, behind
  `\r` separators, MySQL executable comments `/*!…*/`, `xp_cmdshell`,
  `pg_read_file`, and `SEL/**/ECT` splicing.
- **The executed string is always the validated string.** Proven by tracing at
  the database driver level — not by trusting the code's own claim.
- **The prompt is byte-identical** to OmniSQL's published template: 0 differences
  across 25 lines.
- **The accuracy denominator is honest** — a model cannot manufacture a "skip"
  to inflate its score.

**Three real bugs it did find, now fixed:**

**6. The evaluation was secretly grading row order** — the most important finding.
10 of the 15 reference queries had an `ORDER BY` that the question never asked
for (e.g. "What different categories of products do we sell?"). The harness
grades row order whenever the reference is ordered, so a model that returned the
right rows in a different order was marked **wrong**. This would have
*understated* accuracy on every row of your headline table — base model and
fine-tuned alike. Fixed: order is now graded only on the 2 questions that
genuinely ask for a ranking.

**7. Every MySQL query ending in a quoted identifier was being corrupted.** The
code used `str.strip("\`\`\`")` to remove code fences, but that strips
*characters*, not the fence — so it ate the closing backtick of
``SELECT full_name FROM `customers` ``, producing "unterminated quoted
identifier" on a perfectly valid query. The safety layer's own tests passed
because they never went through extraction. Unit test green, real path broken.

**8. A query could silently lose its `WHERE` clause and report success.** The
code cut model output at the first semicolon, without knowing that a semicolon
inside a `-- comment` doesn't end a statement. So
`SELECT name FROM products -- prices in USD; tax excluded` + `WHERE in_stock = 0`
became `SELECT name FROM products` — **11 rows returned instead of 1, shown to
the user as a correct answer.** Same bug also hid stacked-statement injection
attempts from the audit log by trimming them before the safety layer saw them.
Both fixed.

Bugs 7 and 8 are the clearest argument for why the cross-verification was worth
doing: every module's own tests passed, and both bugs lived in the seam
*between* modules, which is exactly where nobody's unit tests look.

---

## 5. What is NOT verified — read this before quoting anything

- **No PostgreSQL server has ever been reached.** Everything is verified against
  SQLite. The `sudo` commands failed because the terminal could not prompt for a
  password.
- **The PostgreSQL, MySQL and SQL Server introspection paths have never run.**
  They are written, not tested. Do not claim they work.
- **`psycopg` and `openai` are not installed**, so the PostgreSQL driver path and
  the vLLM client path are both unexercised.
- **Real OmniSQL has never been loaded.** Everything is on the mock.
- **GPT never replied to the cross-review request.** The mailbox channel works
  and five requests are sitting there `OPEN`, but Codex stayed heads-down on the
  healing engine. The adversarial review of `safety.py` by a second party has not
  happened yet.

---

## 6. What you need to do

**Highest priority — your work is not committed.** GPT ran `git init` and has
committed its own healing work, but everything in Pillar 1 is untracked. One
`git clean -fd` would delete it.

```bash
git add -A && git commit -m "feat(pipeline): NL-to-SQL scaffold, safety layer, eval harness"
```

Then, when you have a terminal that can ask for your password:

```bash
sudo apt install -y python3.12-venv
sudo docker compose up -d
```

After that the PostgreSQL paths can finally be verified, and `medium-02` becomes
gradeable, making the eval 15/15 instead of 14/15.

## 7. Try it yourself

```bash
cd "/home/muhammad-uzair/FYP dudes"
python3 -m scm_agent.eval.eval_harness          # accuracy table
python3 -m scm_agent.pipeline.safety            # 42 security cases
python3 -m scm_agent.pipeline.nl_to_sql         # full pipeline

# and the live API (needs a SQLite copy of the schema first):
SCM_MODEL_PROVIDER=mock SCM_DIALECT=sqlite \
SCM_DB_DSN='sqlite:///<path/to/demo.db>' \
python3 -m uvicorn scm_agent.app.main:app --port 8080
```

## 8. Decisions waiting on you

1. **Sample values in prompts.** `include_samples=True` sends a few real column
   values to the model. It materially improves accuracy, but on a customer
   database that is real data crossing into model context. That is a BO-7
   decision for you and your supervisor, not for an agent.
2. **"Reversible" healing actions.** Your scope doc promises healing actions are
   reversible. You cannot un-VACUUM a table or un-kill a connection. Recommend
   changing the wording to "auditable and non-destructive" before it reaches the
   report. Raise it with Mr. Ahsan Shah.
3. **Multi-dialect scope.** You chose PostgreSQL + MySQL + SQL Server. Only
   SQLite and (soon) PostgreSQL will realistically be *tested*. Decide whether
   the report claims the other two, or lists them as designed-not-verified.
