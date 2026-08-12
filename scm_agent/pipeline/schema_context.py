"""Database introspection -> the `{db_details}` block of the OmniSQL prompt.

This is the module that makes the system schema-agnostic (objective BO-6): you
hand it credentials for a database it has never seen, it reads the catalog and
describes the structure back as `CREATE TABLE` DDL. Nothing about any schema is
baked into the model — it is re-read on every request.

The output format is fixed by OmniSQL's published prompt template, whose
`Database Schema:` section expects:

    CREATE TABLE statements (DDL) of the database tables, optionally including
    database values and column descriptions via SQL comments.

so `to_ddl()` emits exactly that, with optional sample values as trailing
comments. Sample values matter more than they look: they teach the model that
`status` holds 'delivered' rather than 'DELIVERED', which is a common source of
otherwise-perfect queries returning zero rows.

Verification status of each dialect (see docs/TESTING.md):

    sqlite      -- verified, exercised by the self-test at the bottom
    postgresql  -- implemented; verify once the 5434 container is running
    mysql       -- implemented, NOT yet run against a real server
    sqlserver   -- implemented, NOT yet run against a real server

Connections are plain DB-API 2.0 objects, so `sqlite3.Connection` and
`psycopg.Connection` both work here without this module importing either driver.
"""

from __future__ import annotations

from dataclasses import dataclass

from scm_agent.pipeline.config import Dialect

# Catalog schema to introspect when the caller does not name one.
# MySQL has no separate schema layer, so it resolves to the current database.
DEFAULT_SCHEMA: dict[Dialect, str | None] = {
    Dialect.POSTGRESQL: "public",
    Dialect.SQLSERVER: "dbo",
    Dialect.MYSQL: None,
    Dialect.SQLITE: None,
}


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    default: str | None
    is_primary_key: bool


@dataclass(frozen=True)
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...]

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_primary_key)


@dataclass(frozen=True)
class DatabaseSchema:
    dialect: Dialect
    tables: tuple[Table, ...]

    def to_ddl(self, samples: dict[str, dict[str, list[str]]] | None = None) -> str:
        """Render as CREATE TABLE statements for the prompt's `{db_details}`.

        `samples` maps table -> column -> example values, embedded as trailing
        SQL comments. Omit it for a structure-only rendering.
        """
        return "\n\n".join(_render_table(t, self.dialect, samples) for t in self.tables)

    def summary(self) -> dict[str, object]:
        """Compact view for the /schema endpoint and for logging."""
        return {
            "dialect": self.dialect.value,
            "table_count": len(self.tables),
            "tables": {
                t.name: {
                    "columns": [c.name for c in t.columns],
                    "primary_key": list(t.primary_key),
                    "foreign_keys": [
                        f"{fk.column} -> {fk.ref_table}.{fk.ref_column}"
                        for fk in t.foreign_keys
                    ],
                }
                for t in self.tables
            },
        }


# --------------------------------------------------------------------------
# Identifier quoting
# --------------------------------------------------------------------------

def quote_ident(name: str, dialect: Dialect) -> str:
    """Quote an identifier for the target dialect.

    Kept here rather than in config.py so the protected swap-point file stays
    untouched. Doubling the closing character is the standard escape in every
    dialect we support, which also blocks identifier-based injection.
    """
    if dialect is Dialect.MYSQL:
        return "`" + name.replace("`", "``") + "`"
    if dialect is Dialect.SQLSERVER:
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------

def introspect(
    conn,
    dialect: Dialect,
    schema: str | None = None,
    include_samples: bool = False,
    sample_limit: int = 3,
) -> tuple[DatabaseSchema, dict[str, dict[str, list[str]]]]:
    """Read the catalog and return (schema, samples).

    `samples` is empty unless `include_samples` is set. Sampling reads real row
    values, so it is opt-in — on a production database the caller may not want
    values echoed into a prompt.
    """
    if schema is None:
        schema = DEFAULT_SCHEMA[dialect]

    if dialect is Dialect.SQLITE:
        db = _introspect_sqlite(conn)
    else:
        db = _introspect_information_schema(conn, dialect, schema)

    samples: dict[str, dict[str, list[str]]] = {}
    if include_samples:
        samples = _collect_samples(conn, db, schema, sample_limit)
    return db, samples


def _introspect_sqlite(conn) -> DatabaseSchema:
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    table_names = [r[0] for r in cur.fetchall()]

    tables: list[Table] = []
    for name in table_names:
        # PRAGMA does not accept bind parameters. The name came from
        # sqlite_master rather than user input, but quote it anyway.
        cur.execute(f"PRAGMA table_info({quote_ident(name, Dialect.SQLITE)})")
        columns = tuple(
            Column(
                name=row[1],
                data_type=row[2] or "TEXT",
                nullable=not row[3],
                default=row[4],
                is_primary_key=bool(row[5]),
            )
            for row in cur.fetchall()
        )

        cur.execute(f"PRAGMA foreign_key_list({quote_ident(name, Dialect.SQLITE)})")
        foreign_keys = tuple(
            ForeignKey(column=row[3], ref_table=row[2], ref_column=row[4])
            for row in cur.fetchall()
        )
        tables.append(Table(name=name, columns=columns, foreign_keys=foreign_keys))

    return DatabaseSchema(dialect=Dialect.SQLITE, tables=tuple(tables))


def _introspect_information_schema(
    conn, dialect: Dialect, schema: str | None
) -> DatabaseSchema:
    """Shared path for PostgreSQL / MySQL / SQL Server.

    `information_schema` is standardised for tables, columns and primary keys.
    Foreign keys are the part each vendor exposes differently, so that query is
    selected per dialect below.
    """
    cur = conn.cursor()
    ph = "%s"  # all three drivers we target use pyformat/format placeholders

    if dialect is Dialect.MYSQL and schema is None:
        cur.execute("SELECT DATABASE()")
        schema = cur.fetchone()[0]

    cur.execute(
        f"""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = {ph} AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    table_names = [r[0] for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT table_name, column_name, data_type, is_nullable, column_default,
               character_maximum_length, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = {ph}
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    columns_by_table: dict[str, list[tuple]] = {}
    for row in cur.fetchall():
        columns_by_table.setdefault(row[0], []).append(row)

    cur.execute(
        f"""
        SELECT kcu.table_name, kcu.column_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = {ph}
        """,
        (schema,),
    )
    pk_columns: set[tuple[str, str]] = {(r[0], r[1]) for r in cur.fetchall()}

    fk_rows = _fetch_foreign_keys(cur, dialect, schema, ph)
    fks_by_table: dict[str, list[ForeignKey]] = {}
    for table_name, column, ref_table, ref_column in fk_rows:
        fks_by_table.setdefault(table_name, []).append(
            ForeignKey(column=column, ref_table=ref_table, ref_column=ref_column)
        )

    tables: list[Table] = []
    for name in table_names:
        columns = tuple(
            Column(
                name=row[1],
                data_type=_render_type(row[2], row[5], row[6], row[7]),
                nullable=(row[3] or "").upper() == "YES",
                default=row[4],
                is_primary_key=(name, row[1]) in pk_columns,
            )
            for row in columns_by_table.get(name, [])
        )
        tables.append(
            Table(
                name=name,
                columns=columns,
                foreign_keys=tuple(fks_by_table.get(name, [])),
            )
        )

    return DatabaseSchema(dialect=dialect, tables=tuple(tables))


def _fetch_foreign_keys(cur, dialect: Dialect, schema: str | None, ph: str) -> list[tuple]:
    """Foreign keys — the one part of information_schema vendors disagree on."""
    if dialect is Dialect.POSTGRESQL:
        cur.execute(
            f"""
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS ref_table, ccu.column_name AS ref_column
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = {ph}
            ORDER BY tc.table_name, kcu.ordinal_position
            """,
            (schema,),
        )
        return list(cur.fetchall())

    if dialect is Dialect.MYSQL:
        # MySQL exposes the target directly on key_column_usage.
        cur.execute(
            f"""
            SELECT table_name, column_name,
                   referenced_table_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = {ph} AND referenced_table_name IS NOT NULL
            ORDER BY table_name, ordinal_position
            """,
            (schema,),
        )
        return list(cur.fetchall())

    if dialect is Dialect.SQLSERVER:
        cur.execute(
            f"""
            SELECT fk.table_name, fk.column_name,
                   pk.table_name AS ref_table, pk.column_name AS ref_column
            FROM information_schema.referential_constraints AS rc
            JOIN information_schema.key_column_usage AS fk
              ON fk.constraint_name = rc.constraint_name
            JOIN information_schema.key_column_usage AS pk
              ON pk.constraint_name = rc.unique_constraint_name
             AND pk.ordinal_position = fk.ordinal_position
            WHERE fk.table_schema = {ph}
            ORDER BY fk.table_name, fk.ordinal_position
            """,
            (schema,),
        )
        return list(cur.fetchall())

    raise NotImplementedError(f"No foreign-key introspection for dialect {dialect}")


def _render_type(data_type: str, char_len, precision, scale) -> str:
    """Turn information_schema type parts back into a readable DDL type."""
    if char_len:
        return f"{data_type}({char_len})"
    if precision and scale is not None and data_type.lower() in {"numeric", "decimal"}:
        return f"{data_type}({precision},{scale})"
    return data_type


def _collect_samples(
    conn, db: DatabaseSchema, schema: str | None, limit: int
) -> dict[str, dict[str, list[str]]]:
    """Read a few distinct values per column, for the DDL comments.

    Best-effort: a permission error on one table must not fail introspection of
    the whole database, so every read is individually guarded.
    """
    out: dict[str, dict[str, list[str]]] = {}
    cur = conn.cursor()
    for table in db.tables:
        qualified = quote_ident(table.name, db.dialect)
        if schema:
            qualified = f"{quote_ident(schema, db.dialect)}.{qualified}"
        for column in table.columns:
            col = quote_ident(column.name, db.dialect)
            if db.dialect is Dialect.SQLSERVER:
                sql = (
                    f"SELECT DISTINCT TOP {limit} {col} FROM {qualified} "
                    f"WHERE {col} IS NOT NULL"
                )
            else:
                sql = (
                    f"SELECT DISTINCT {col} FROM {qualified} "
                    f"WHERE {col} IS NOT NULL LIMIT {limit}"
                )
            try:
                cur.execute(sql)
                values = [_format_value(r[0]) for r in cur.fetchall()]
            except Exception:
                continue
            if values:
                out.setdefault(table.name, {})[column.name] = values
    return out


def _format_value(value) -> str:
    text = str(value)
    if len(text) > 40:
        text = text[:37] + "..."
    return text.replace("\n", " ")


# --------------------------------------------------------------------------
# DDL rendering
# --------------------------------------------------------------------------

def _render_table(
    table: Table,
    dialect: Dialect,
    samples: dict[str, dict[str, list[str]]] | None,
) -> str:
    table_samples = (samples or {}).get(table.name, {})
    lines: list[str] = [f"CREATE TABLE {quote_ident(table.name, dialect)} ("]

    parts: list[str] = []
    for column in table.columns:
        piece = f"  {quote_ident(column.name, dialect)} {column.data_type}"
        if not column.nullable:
            piece += " NOT NULL"
        parts.append(piece)

    if table.primary_key:
        cols = ", ".join(quote_ident(c, dialect) for c in table.primary_key)
        parts.append(f"  PRIMARY KEY ({cols})")

    for fk in table.foreign_keys:
        parts.append(
            f"  FOREIGN KEY ({quote_ident(fk.column, dialect)}) "
            f"REFERENCES {quote_ident(fk.ref_table, dialect)}"
            f"({quote_ident(fk.ref_column, dialect)})"
        )

    # Comments go after the comma so the DDL stays syntactically valid if it is
    # ever executed rather than just shown to the model.
    rendered: list[str] = []
    for index, piece in enumerate(parts):
        suffix = "," if index < len(parts) - 1 else ""
        comment = ""
        column_name = _column_for_part(piece, table, dialect)
        if column_name and column_name in table_samples:
            comment = "  -- e.g. " + ", ".join(table_samples[column_name])
        rendered.append(f"{piece}{suffix}{comment}")

    lines.extend(rendered)
    lines.append(");")
    return "\n".join(lines)


def _column_for_part(piece: str, table: Table, dialect: Dialect) -> str | None:
    """Match a rendered line back to its column, to attach the right comment."""
    stripped = piece.strip()
    for column in table.columns:
        if stripped.startswith(quote_ident(column.name, dialect)):
            return column.name
    return None


if __name__ == "__main__":
    import json
    import sqlite3

    # Self-test against an in-memory SQLite database mirroring db/schema.sql.
    # Synthetic rows only. Runs with zero installs — sqlite3 is stdlib.
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            full_name   VARCHAR(120) NOT NULL,
            email       VARCHAR(160) NOT NULL UNIQUE,
            city        VARCHAR(80),
            country     VARCHAR(80),
            signup_date DATE NOT NULL
        );

        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            name       VARCHAR(160) NOT NULL,
            category   VARCHAR(80) NOT NULL,
            unit_price NUMERIC(10,2) NOT NULL,
            in_stock   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE orders (
            order_id    INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
            order_date  DATE NOT NULL,
            status      VARCHAR(20) NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id      INTEGER NOT NULL REFERENCES orders(order_id),
            product_id    INTEGER NOT NULL REFERENCES products(product_id),
            quantity      INTEGER NOT NULL,
            unit_price    NUMERIC(10,2) NOT NULL
        );

        INSERT INTO customers VALUES
            (1,'Ayesha Karim','ayesha.karim@example.com','Lahore','Pakistan','2024-01-15'),
            (2,'Bilal Ahmed','bilal.ahmed@example.com','Karachi','Pakistan','2024-02-03'),
            (3,'Elena Rossi','elena.rossi@example.com','Milan','Italy','2024-04-02');

        INSERT INTO products VALUES
            (1,'Mechanical Keyboard','Peripherals',89.99,42),
            (2,'27in 4K Monitor','Displays',329.50,15);

        INSERT INTO orders VALUES
            (1,1,'2024-03-01','delivered'),
            (2,2,'2024-08-05','shipped'),
            (3,3,'2024-09-18','pending');

        INSERT INTO order_items VALUES
            (1,1,1,1,89.99),
            (2,2,2,2,329.50);
        """
    )

    print("=" * 70)
    print("STRUCTURE ONLY  (what goes into {db_details})")
    print("=" * 70)
    db, _ = introspect(conn, Dialect.SQLITE)
    print(db.to_ddl())

    print()
    print("=" * 70)
    print("WITH SAMPLE VALUES  (include_samples=True)")
    print("=" * 70)
    db, samples = introspect(conn, Dialect.SQLITE, include_samples=True)
    print(db.to_ddl(samples))

    print()
    print("=" * 70)
    print("SUMMARY  (for the /schema endpoint)")
    print("=" * 70)
    print(json.dumps(db.summary(), indent=2))

    print()
    print("=" * 70)
    print("CHECKS")
    print("=" * 70)
    names = [t.name for t in db.tables]
    print(f"  tables discovered          : {names}")
    assert names == ["customers", "order_items", "orders", "products"], names

    items = next(t for t in db.tables if t.name == "order_items")
    fks = {(f.column, f.ref_table, f.ref_column) for f in items.foreign_keys}
    print(f"  order_items foreign keys   : {sorted(fks)}")
    assert ("order_id", "orders", "order_id") in fks
    assert ("product_id", "products", "product_id") in fks

    orders = next(t for t in db.tables if t.name == "orders")
    print(f"  orders primary key         : {orders.primary_key}")
    assert orders.primary_key == ("order_id",)

    status_samples = samples["orders"]["status"]
    print(f"  sampled orders.status      : {status_samples}")
    assert "delivered" in status_samples

    quoted = quote_ident('weird"name', Dialect.POSTGRESQL)
    print(f"  identifier quoting escapes : {quoted}")
    assert quoted == '"weird""name"'

    print("\n  all assertions passed")
    conn.close()
