"""The model layer — one interface, three stages, zero code changes between them.

`config.py` decides *which* model we talk to. This file is *how* we talk to it.
Everything downstream (`nl_to_sql.py`, `eval/eval_harness.py`, `app/main.py`)
depends only on `get_model_client()` and the single method `generate()`, so the
stage-1 -> stage-2 -> stage-3 swap never reaches them:

    stage 1  SCM_MODEL_PROVIDER=mock                -> MockModelClient
    stage 2  SCM_MODEL_PROVIDER=openai_compatible   -> OpenAICompatibleClient (base OmniSQL on vLLM)
    stage 3  ... + SCM_LORA_ADAPTER=<name>          -> OpenAICompatibleClient (same class, different served_model)

Why the mock is shaped the way it is
------------------------------------
OmniSQL does **not** answer with bare SQL. Its published prompt template ends
with an explicit output contract:

    Output Format:
    In your answer, please enclose the generated SQL query in a code block:
    ```sql
    -- Your SQL query
    ```
    Take a deep breath and think step by step to find the correct SQL query.

So a real response is *prose reasoning first, then a fenced ```sql block*. If the
mock returned clean bare SQL, every downstream component would be developed and
tested against output the real model never produces — the SQL extractor would
look correct today and fall over on the first response from a real GPU, in the
week before the viva. Reproducing the messy shape now is the entire reason this
mock exists.

The exact contract this file guarantees (write your parser against this)
-----------------------------------------------------------------------
    <one or more lines of reasoning prose>
    <blank line>
    ```sql
    <SQL, possibly spanning several lines>
    ```

`SQL_FENCE_OPEN` and `FENCE_CLOSE` below are the literal markers. Import them
rather than re-typing "```sql" in the parser, so there is one place to change if
the template ever moves. A robust parser should still tolerate a bare ``` fence,
extra prose *after* the block, and more than one block (take the last one) —
real models drift, which is exactly what the fine-tune measurement is for.

No third-party import happens at module import time. `openai` is not installed
on this machine, and a top-level `import openai` would break every module that
imports this one, including the ones that only ever use the mock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from scm_agent.pipeline.config import ModelConfig, ModelProvider

# The literal fence markers OmniSQL's output contract specifies. Exported so the
# SQL extractor and this mock cannot drift apart.
SQL_FENCE_OPEN = "```sql"
FENCE_CLOSE = "```"


class ModelClientError(RuntimeError):
    """Raised for problems this project can give an actionable message about.

    Deliberately distinct from the vendor SDK's exceptions: callers should be
    able to catch "our model layer failed" without importing `openai`, which on
    a mock-only machine is not installed at all.
    """


@runtime_checkable
class ModelClient(Protocol):
    """The whole interface. One method, on purpose.

    A Protocol rather than an ABC because the implementations share no state and
    nothing benefits from inheritance — but `@runtime_checkable` still lets the
    self-test (and any future test) assert that a client satisfies the contract.
    """

    def generate(self, prompt: str) -> str:
        """Return the model's raw, unparsed text for `prompt`.

        Raw is the point: parsing belongs to the caller, so the caller is forced
        to handle the real reasoning-then-fenced-block shape.
        """
        ...


# --------------------------------------------------------------------------
# Stage 1 — the mock
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _CannedAnswer:
    """One rule: when the question looks like X, reason like Y, answer with Z.

    `keyword_groups` is an AND of ORs — every group must contribute at least one
    hit for the rule to fire. That is enough structure to keep "how many orders
    does each customer have" (needs *order* AND *customer*) from colliding with
    "how many customers are there" (needs *customer* only), without pulling in a
    dependency or any randomness.
    """

    name: str
    keyword_groups: tuple[tuple[str, ...], ...]
    reasoning: tuple[str, ...]
    sql: str

    def matches(self, question: str) -> bool:
        return all(
            any(keyword in question for keyword in group)
            for group in self.keyword_groups
        )

    def render(self) -> str:
        """Assemble the reasoning-then-fenced-block shape a real model emits."""
        prose = "\n".join(self.reasoning)
        return f"{prose}\n\n{SQL_FENCE_OPEN}\n{self.sql.strip()}\n{FENCE_CLOSE}"


# Ordered most-specific first: the first rule that matches wins, which is what
# makes the mock deterministic. The SQL is deliberately ANSI-portable so it runs
# unchanged on SQLite (what we can test with today) and PostgreSQL (the real
# target). SQL Server would need TOP instead of LIMIT — acceptable, because the
# mock only ever exists to unblock local, GPU-free development.
_CANNED_ANSWERS: tuple[_CannedAnswer, ...] = (
    _CannedAnswer(
        name="top_selling_product",
        keyword_groups=(
            ("top", "best", "most", "highest", "biggest"),
            ("sell", "sold", "selling", "sale", "popular"),
        ),
        reasoning=(
            "The quantities sold live in `order_items`, but the product name lives in "
            "`products`, so I need to join the two on `product_id`.",
            "Summing `quantity` per product and ordering that total descending puts the "
            "best seller first.",
            "I group by both `product_id` and `name` so the name is legal to select, then "
            "take only the first row.",
        ),
        sql=(
            "SELECT p.name, SUM(oi.quantity) AS units_sold\n"
            "FROM order_items AS oi\n"
            "JOIN products AS p ON p.product_id = oi.product_id\n"
            "GROUP BY p.product_id, p.name\n"
            "ORDER BY units_sold DESC\n"
            "LIMIT 1;"
        ),
    ),
    _CannedAnswer(
        name="orders_per_customer",
        keyword_groups=(
            ("order",),
            ("customer", "client", "buyer"),
        ),
        reasoning=(
            "This needs one row per customer, so `customers` is the driving table and "
            "`orders` is joined on `customer_id`.",
            "A LEFT JOIN matters here: an INNER JOIN would silently drop customers who "
            "have never ordered, which is usually the interesting group.",
            "Counting `o.order_id` rather than `*` keeps those customers at zero instead "
            "of one.",
        ),
        sql=(
            "SELECT c.customer_id, c.full_name, COUNT(o.order_id) AS order_count\n"
            "FROM customers AS c\n"
            "LEFT JOIN orders AS o ON o.customer_id = c.customer_id\n"
            "GROUP BY c.customer_id, c.full_name\n"
            "ORDER BY order_count DESC;"
        ),
    ),
    _CannedAnswer(
        name="total_revenue",
        keyword_groups=(
            ("revenue", "total sales", "turnover", "income", "earned", "money"),
        ),
        reasoning=(
            "Revenue is not stored anywhere as a column — it has to be computed from "
            "`order_items`.",
            "Each line item is worth `quantity * unit_price`, and `order_items.unit_price` "
            "is the price captured at order time, which is the correct one to use rather "
            "than today's `products.unit_price`.",
            "Summing that product across all line items gives the total.",
        ),
        sql=(
            "SELECT SUM(oi.quantity * oi.unit_price) AS total_revenue\n"
            "FROM order_items AS oi;"
        ),
    ),
    _CannedAnswer(
        name="products_by_price",
        keyword_groups=(
            ("product", "item"),
            ("price", "expensive", "cost", "cheap", "priciest"),
        ),
        reasoning=(
            "Everything needed is in `products`, so no join is required.",
            "I select the name and `unit_price` and sort by price descending so the most "
            "expensive products appear first.",
        ),
        sql=(
            "SELECT name, category, unit_price\n"
            "FROM products\n"
            "ORDER BY unit_price DESC;"
        ),
    ),
    _CannedAnswer(
        name="count_customers",
        keyword_groups=(
            ("how many", "count", "number of", "total"),
            ("customer", "client", "buyer"),
        ),
        reasoning=(
            "Each row in `customers` is exactly one customer, so this is a single-table "
            "aggregate with no join and no filter.",
            "COUNT(*) over that table is the answer.",
        ),
        sql="SELECT COUNT(*) AS customer_count\nFROM customers;",
    ),
)

# Used when nothing matches. Still emits the full reasoning-then-fence shape, so
# the parser is exercised even on the unhappy path.
_FALLBACK_ANSWER = _CannedAnswer(
    name="fallback",
    keyword_groups=(),
    reasoning=(
        "I could not tie this question to a specific metric in the schema, so I will "
        "return a small sample of the primary entity table instead of guessing.",
        "`customers` is the natural starting point, and a LIMIT keeps the result "
        "readable.",
    ),
    sql="SELECT *\nFROM customers\nLIMIT 10;",
)

# Section headings in OmniSQL's prompt template. Used to know where the question
# stops, so the mock never keyword-matches against the schema DDL — which
# contains the words "customers", "orders", "products" and "unit_price" and
# would otherwise match every rule at once.
_SECTION_HEADERS = (
    "task overview:",
    "database engine:",
    "database schema:",
    "instructions:",
    "output format:",
)


def extract_question(prompt: str) -> str:
    """Pull the natural-language question out of a full OmniSQL prompt.

    Falls back to the entire prompt when there is no `Question:` marker, so the
    mock is equally usable with a bare question string during development.
    """
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.lower().startswith("question:"):
            continue
        collected = [stripped[len("question:"):].strip()]
        for follow in lines[index + 1:]:
            candidate = follow.strip()
            if not candidate or candidate.lower() in _SECTION_HEADERS:
                break
            collected.append(candidate)
        question = " ".join(part for part in collected if part).strip()
        if question:
            return question
    return prompt.strip()


@dataclass(frozen=True)
class MockModelClient:
    """A GPU-free stand-in that imitates OmniSQL's *output shape*, not its skill.

    It is a lookup table with a keyword matcher, and that is fine — its job is to
    let the safety layer, the executor, the API and the eval harness be built and
    tested end to end while the real model is still blocked on compute.

    Deterministic by construction: same prompt in, byte-identical text out, no
    clock, no randomness. `eval/eval_harness.py` depends on this — a mock that
    varied would produce an accuracy score that changed between runs and would be
    worthless as the baseline row of the report's comparison table.
    """

    config: ModelConfig | None = None

    def generate(self, prompt: str) -> str:
        question = extract_question(prompt).lower()
        for answer in _CANNED_ANSWERS:
            if answer.matches(question):
                return answer.render()
        return _FALLBACK_ANSWER.render()

    def match_name(self, prompt: str) -> str:
        """Which canned rule fired. For test output and debugging only."""
        question = extract_question(prompt).lower()
        for answer in _CANNED_ANSWERS:
            if answer.matches(question):
                return answer.name
        return _FALLBACK_ANSWER.name


# --------------------------------------------------------------------------
# Stages 2 and 3 — a real model behind an OpenAI-compatible endpoint
# --------------------------------------------------------------------------

def _load_openai_class():
    """Import `openai` lazily and turn its absence into a useful message.

    Lazy on purpose. `openai` is not installed on the development machine, and a
    module-level import would make `import model_client` fail for everyone —
    including the mock path, which needs no network and no dependencies at all.

    The raw ImportError ("No module named 'openai'") does not tell a student what
    to do about it, so it is translated here into instructions that name the
    actual blocker on this machine (PEP 668 / missing python3.12-venv).
    """
    try:
        from openai import OpenAI  # noqa: PLC0415 — deliberately deferred
    except ImportError as exc:
        raise ModelClientError(
            "The 'openai' package is required for "
            "SCM_MODEL_PROVIDER=openai_compatible but is not installed.\n"
            "  Install it into the project virtualenv:\n"
            "      sudo apt install -y python3.12-venv   # once, if missing\n"
            "      python3 -m venv .venv\n"
            "      .venv/bin/pip install openai\n"
            "  Or stay on the GPU-free path for now with "
            "SCM_MODEL_PROVIDER=mock."
        ) from exc
    return OpenAI


class OpenAICompatibleClient:
    """Talks to vLLM (or any OpenAI-compatible server) serving OmniSQL.

    Not a frozen dataclass, unlike the rest of this codebase, because it owns a
    live SDK handle — an object with sockets and retry state, which is not the
    kind of thing an immutable value object should carry.

    Stages 2 and 3 are the *same class*. vLLM addresses a loaded LoRA adapter as
    just another model name, and `ModelConfig.served_model` already resolves to
    the adapter name when one is configured, so swapping the base model for our
    fine-tune changes nothing here.
    """

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        openai_class = _load_openai_class()
        # Constructed eagerly so a missing package or a missing base URL fails at
        # startup, matching config.validate()'s philosophy — a request-time
        # failure during the live demo is the worst possible time to find out.
        self._client = openai_class(
            base_url=config.base_url,
            api_key=config.api_key,
        )

    def generate(self, prompt: str) -> str:
        # Chat completions, with the whole OmniSQL prompt as a single user
        # message and no system message. That is how the model card uses it, and
        # the prompt already carries its own task description — adding a system
        # prompt here would make inference differ from the format the training
        # data is generated in (train/serve skew), which docs/PROJECT_BRIEF.md
        # explicitly forbids.
        try:
            response = self._client.chat.completions.create(
                model=self.config.served_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        except Exception as exc:  # SDK exception types vary by version
            raise ModelClientError(
                f"Request to {self.config.base_url!r} for model "
                f"{self.config.served_model!r} failed: {exc}\n"
                "  Check that the vLLM server is running and reachable, e.g.\n"
                "      curl "
                f"{(self.config.base_url or 'http://localhost:8000/v1').rstrip('/')}/models"
            ) from exc

        if not response.choices:
            raise ModelClientError(
                f"Model {self.config.served_model!r} returned no choices — "
                "the server accepted the request but produced nothing."
            )
        return response.choices[0].message.content or ""


# --------------------------------------------------------------------------
# Factory — the only entry point the rest of the codebase should use
# --------------------------------------------------------------------------

def get_model_client(config: ModelConfig | None = None) -> ModelClient:
    """Build the client the environment asks for.

    Callers pass no argument in production and read the environment; tests pass
    an explicit `ModelConfig` so they never depend on the developer's shell.
    """
    if config is None:
        config = ModelConfig.from_env()
    config.validate()

    if config.provider is ModelProvider.MOCK:
        return MockModelClient(config)
    if config.provider is ModelProvider.OPENAI_COMPATIBLE:
        return OpenAICompatibleClient(config)

    # Unreachable while ModelProvider has two members, but if someone adds a
    # third they should get a clear message rather than a silent None.
    raise ModelClientError(f"No client implemented for provider {config.provider!r}")


if __name__ == "__main__":
    import os
    import sys

    # ------------------------------------------------------------------
    # Self-test. Runs with zero installs: the mock path touches no network
    # and no third-party package.
    # ------------------------------------------------------------------
    PROMPTS = [
        "How many customers are there?",
        "List all products by price",
        "What is the total revenue?",
        "How many orders does each customer have?",
        "Which product sells the most?",
        "Tell me something interesting about the database",
    ]

    os.environ["SCM_MODEL_PROVIDER"] = "mock"
    client = get_model_client()
    assert isinstance(client, MockModelClient), type(client)
    assert isinstance(client, ModelClient), "MockModelClient must satisfy the Protocol"
    print(f"factory returned: {type(client).__name__} "
          f"(stage = {ModelConfig.from_env().stage})\n")

    print("=" * 70)
    print("RAW MOCK OUTPUT  (this is the shape the SQL parser must handle)")
    print("=" * 70)
    for prompt in PROMPTS:
        text = client.generate(prompt)
        print(f"\n--- prompt: {prompt!r}  ->  rule: {client.match_name(prompt)}")
        print(text)

    print()
    print("=" * 70)
    print("A FULL OmniSQL-STYLE PROMPT  (question must be found inside it)")
    print("=" * 70)
    full_prompt = """Task Overview:
You are a data science expert. Below, you are provided with a database schema
and a natural language question. Your task is to understand the schema and
generate a valid SQL query to answer the question.

Database Engine:
PostgreSQL

Database Schema:
CREATE TABLE "customers" (
  "customer_id" integer NOT NULL,
  "full_name" character varying(120) NOT NULL,
  PRIMARY KEY ("customer_id")
);

CREATE TABLE "orders" (
  "order_id" integer NOT NULL,
  "customer_id" integer NOT NULL,
  PRIMARY KEY ("order_id"),
  FOREIGN KEY ("customer_id") REFERENCES "customers"("customer_id")
);

Question:
How many customers are there?

Instructions:
- Make sure you only output the information that is asked in the question.

Output Format:
In your answer, please enclose the generated SQL query in a code block:
```sql
-- Your SQL query
```

Take a deep breath and think step by step to find the correct SQL query.
"""
    print(f"extracted question: {extract_question(full_prompt)!r}")
    print(f"rule fired        : {client.match_name(full_prompt)}")
    assert extract_question(full_prompt) == "How many customers are there?"
    # The schema block names customers, orders and products. If the matcher read
    # the whole prompt it would fire the wrong rule — this is the regression that
    # check protects against.
    assert client.match_name(full_prompt) == "count_customers"
    print("\n" + client.generate(full_prompt))

    print()
    print("=" * 70)
    print("CHECKS")
    print("=" * 70)

    for prompt in PROMPTS:
        text = client.generate(prompt)
        assert SQL_FENCE_OPEN in text, f"no ```sql fence for {prompt!r}"
        assert text.rstrip().endswith(FENCE_CLOSE), f"fence not closed for {prompt!r}"
        body = text.split(SQL_FENCE_OPEN, 1)[1].split(FENCE_CLOSE, 1)[0].strip()
        assert body.upper().startswith("SELECT"), body
        assert text.index(SQL_FENCE_OPEN) > 0, "reasoning must precede the fence"
    print(f"  fenced ```sql block present in all {len(PROMPTS)} responses : yes")
    print("  reasoning prose precedes every fence                    : yes")

    for prompt in PROMPTS:
        assert client.generate(prompt) == client.generate(prompt), prompt
    fresh = MockModelClient()
    assert fresh.generate(PROMPTS[0]) == client.generate(PROMPTS[0])
    print("  identical prompt -> identical text (2 calls, 2 clients)  : yes")

    expected_rules = {
        "How many customers are there?": "count_customers",
        "List all products by price": "products_by_price",
        "What is the total revenue?": "total_revenue",
        "How many orders does each customer have?": "orders_per_customer",
        "Which product sells the most?": "top_selling_product",
        "Tell me something interesting about the database": "fallback",
    }
    for prompt, expected in expected_rules.items():
        actual = client.match_name(prompt)
        assert actual == expected, f"{prompt!r}: expected {expected}, got {actual}"
    print(f"  each of the {len(expected_rules)} question types hits its own rule    : yes")

    # Missing-dependency path. `openai` really is absent here, but poisoning
    # sys.modules makes this check valid on a machine where it is installed too.
    print()
    print("  requesting openai_compatible without the openai package:")
    saved = sys.modules.get("openai", "__absent__")
    sys.modules["openai"] = None  # type: ignore[assignment]
    try:
        get_model_client(
            ModelConfig(
                provider=ModelProvider.OPENAI_COMPATIBLE,
                model_name="seeklhy/OmniSQL-7B",
                base_url="http://localhost:8000/v1",
                api_key="EMPTY",
                lora_adapter=None,
                temperature=0.0,
                max_tokens=1024,
            )
        )
        raise AssertionError("expected ModelClientError for the missing package")
    except ModelClientError as exc:
        assert "openai" in str(exc) and "pip install" in str(exc), str(exc)
        for line in str(exc).splitlines():
            print(f"    {line}")
    finally:
        if saved == "__absent__":
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = saved  # type: ignore[assignment]

    # Config errors must still surface as ValueError from config.validate(),
    # before the client is ever constructed.
    try:
        get_model_client(
            ModelConfig(
                provider=ModelProvider.OPENAI_COMPATIBLE,
                model_name="seeklhy/OmniSQL-7B",
                base_url=None,
                api_key="EMPTY",
                lora_adapter=None,
                temperature=0.0,
                max_tokens=1024,
            )
        )
        raise AssertionError("expected ValueError for the missing base URL")
    except ValueError as exc:
        print(f"\n  missing base URL rejected before any client is built:\n    {exc}")

    print("\n  all assertions passed")
    os.environ.pop("SCM_MODEL_PROVIDER", None)
