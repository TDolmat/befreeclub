#!/usr/bin/env python3
"""Migracja danych: stara baza admina `bfc_admin` (schemat public, Hono+Drizzle)
-> nowa baza `befreeclub` (schematy `admin` i `circle_dm`, FastAPI+alembic).

Skrypt TYLKO kopiuje dane. DDL nowej bazy stawia alembic nowego backendu
(`alembic upgrade head`) - musi być odpalony PRZED tym skryptem.

Uruchomienie:
    uv run --with asyncpg python migrate_data.py [--dry-run] [--truncate] \
        [--source-dsn DSN] [--target-dsn DSN]

Zachowanie:
- kopiuje wiersze z zachowaniem id (COPY binarny przez asyncpg),
- circle_dm.settings (singleton id=1, seedowany przez alembic): upsert zamiast INSERT,
- po kopii ustawia sekwencje (setval na max(id)) dla każdej kolumny serial/identity,
- na końcu weryfikuje: count, max(id), count per status, min/max created_at,
- całość zapisu w JEDNEJ transakcji docelowej; błąd albo rozjazd w weryfikacji
  = ROLLBACK, nowa baza zostaje nietknięta,
- źródło czytane w jednej transakcji REPEATABLE READ (spójny snapshot).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from urllib.parse import urlparse

import asyncpg

DEFAULT_SOURCE_DSN = "postgresql://tomasz@localhost:5432/bfc_admin"
DEFAULT_TARGET_DSN = "postgresql://tomasz@localhost:5432/befreeclub"
BATCH_SIZE = 1000  # wierszy na jeden COPY (kb_documents ma duże wiersze base64)

# ---------------------------------------------------------------------------
# MAPA MIGRACJI
# stara tabela (public.*) -> (nowy schemat, nowa tabela, mapa zmienionych kolumn
# stara->nowa). Kolumny spoza mapy przechodzą 1:1 po nazwie (introspekcja).
# KOLEJNOŚĆ = kolejność kopiowania, rodzice przed dziećmi (FK wg docs/spec/db-schema.md):
#   users <- sessions, feedback_items, assistant_conversations(user_id)
#   accounts <- members, threads, kb_documents
#   threads <- messages, draft_sessions, sent_messages, checkups
#   messages <- message_image_descriptions
#   draft_sessions <- draft_iterations, sent_messages(draft_session_id, SET NULL)
#   assistant_conversations <- assistant_messages
# ---------------------------------------------------------------------------
MAPPING: list[tuple[str, str, str, dict[str, str]]] = [
    ("auth_accounts",              "admin",     "users",                      {}),
    ("auth_sessions",              "admin",     "sessions",                   {"auth_account_id": "user_id"}),
    ("feedback_items",             "admin",     "feedback_items",             {"auth_account_id": "user_id"}),
    ("admin_accounts",             "circle_dm", "accounts",                   {}),
    ("community_members",          "circle_dm", "members",                    {"admin_account_id": "account_id"}),
    ("dm_threads",                 "circle_dm", "threads",                    {"admin_account_id": "account_id"}),
    ("dm_messages",                "circle_dm", "messages",                   {}),
    ("message_image_descriptions", "circle_dm", "message_image_descriptions", {}),
    ("draft_sessions",             "circle_dm", "draft_sessions",             {}),
    ("draft_iterations",           "circle_dm", "draft_iterations",           {}),
    ("sent_messages",              "circle_dm", "sent_messages",              {}),
    ("thread_checkups",            "circle_dm", "checkups",                   {}),
    ("app_settings",               "circle_dm", "settings",                   {}),
    ("kb_documents",               "circle_dm", "kb_documents",               {"admin_account_id": "account_id"}),
    ("assistant_conversations",    "circle_dm", "assistant_conversations",    {"auth_account_id": "user_id"}),
    ("assistant_messages",         "circle_dm", "assistant_messages",         {}),
]

# Tabele kopiowane upsertem (INSERT ... ON CONFLICT (id) DO UPDATE) zamiast COPY.
# circle_dm.settings: alembic seeduje wiersz id=1, zwykły INSERT wywaliłby się na PK.
UPSERT_TABLES = {"app_settings"}

NUMERIC_ID_TYPES = ("bigint", "integer", "smallint")


class VerificationMismatch(Exception):
    """Rozjazd w weryfikacji - wymusza rollback transakcji docelowej."""


@dataclass
class TablePlan:
    src_table: str
    tgt_schema: str
    tgt_table: str
    src_cols: list[str]                # kolumny źródłowe (kolejność kopiowania)
    tgt_cols: list[str]                # odpowiadające kolumny docelowe (ta sama kolejność)
    tgt_all_cols: list[str]            # wszystkie kolumny docelowe (do setval)
    src_types: dict[str, str]          # nazwa kolumny źródłowej -> data_type
    src_count: int = 0
    upsert: bool = False
    renamed: dict[str, str] = field(default_factory=dict)
    defaulted_tgt_cols: list[str] = field(default_factory=list)  # bez źródła, mają default

    @property
    def src_qualified(self) -> str:
        return f'public.{q(self.src_table)}'

    @property
    def tgt_qualified(self) -> str:
        return f"{q(self.tgt_schema)}.{q(self.tgt_table)}"

    @property
    def label(self) -> str:
        return f"{self.src_table} -> {self.tgt_schema}.{self.tgt_table}"


def q(ident: str) -> str:
    """Quote identyfikatora SQL."""
    return '"' + ident.replace('"', '""') + '"'


async def fetch_columns(conn: asyncpg.Connection, schema: str, table: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable, column_default, is_identity, is_generated
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """,
        schema,
        table,
    )


# ---------------------------------------------------------------------------
# Budowa planu (introspekcja + walidacja mapowania kolumn)
# ---------------------------------------------------------------------------

async def build_plans(
    src: asyncpg.Connection, tgt: asyncpg.Connection
) -> tuple[list[TablePlan], list[str]]:
    plans: list[TablePlan] = []
    errors: list[str] = []

    for src_table, tgt_schema, tgt_table, renames in MAPPING:
        src_info = await fetch_columns(src, "public", src_table)
        tgt_info = await fetch_columns(tgt, tgt_schema, tgt_table)

        if not src_info:
            errors.append(f"Źródło: tabela public.{src_table} nie istnieje w starej bazie.")
            continue
        if not tgt_info:
            errors.append(
                f"Cel: tabela {tgt_schema}.{tgt_table} nie istnieje. "
                f"Najpierw odpal 'alembic upgrade head' w nowym backendzie."
            )
            continue

        tgt_by_name = {r["column_name"]: r for r in tgt_info}
        src_cols: list[str] = []
        tgt_cols: list[str] = []
        table_errors: list[str] = []

        for r in src_info:
            s_col = r["column_name"]
            t_col = renames.get(s_col, s_col)
            t_info = tgt_by_name.get(t_col)
            if t_info is None:
                table_errors.append(
                    f"  - stara kolumna {src_table}.{s_col} nie ma odpowiednika "
                    f"w {tgt_schema}.{tgt_table} (oczekiwano: {t_col}). "
                    f"Dane by przepadły. Uzupełnij MAPPING albo popraw DDL."
                )
                continue
            if t_info["is_generated"] == "ALWAYS":
                table_errors.append(
                    f"  - {tgt_schema}.{tgt_table}.{t_col} to kolumna GENERATED ALWAYS, "
                    f"a stara {src_table}.{s_col} niesie dane. Nie da się skopiować 1:1."
                )
                continue
            src_cols.append(s_col)
            tgt_cols.append(t_col)

        covered = set(tgt_cols)
        defaulted: list[str] = []
        for r in tgt_info:
            t_col = r["column_name"]
            if t_col in covered or r["is_generated"] == "ALWAYS":
                continue
            if r["column_default"] is not None or r["is_identity"] == "YES":
                defaulted.append(t_col)  # wypełni się defaultem, tylko odnotuj
                continue
            table_errors.append(
                f"  - nowa kolumna {tgt_schema}.{tgt_table}.{t_col} nie ma źródła "
                f"w public.{src_table} ani defaultu. Uzupełnij MAPPING albo dodaj default w DDL."
            )

        if table_errors:
            errors.append(f"{src_table} -> {tgt_schema}.{tgt_table}:\n" + "\n".join(table_errors))
            continue

        src_count = await src.fetchval(f"SELECT count(*) FROM public.{q(src_table)}")
        plans.append(
            TablePlan(
                src_table=src_table,
                tgt_schema=tgt_schema,
                tgt_table=tgt_table,
                src_cols=src_cols,
                tgt_cols=tgt_cols,
                tgt_all_cols=[r["column_name"] for r in tgt_info],
                src_types={r["column_name"]: r["data_type"] for r in src_info},
                src_count=src_count,
                upsert=src_table in UPSERT_TABLES,
                renamed={s: t for s, t in zip(src_cols, tgt_cols) if s != t},
                defaulted_tgt_cols=defaulted,
            )
        )

    return plans, errors


def print_plan(plans: list[TablePlan], truncate: bool) -> None:
    print("\nPlan migracji (kolejność = kolejność kopiowania):\n")
    w = max(len(p.label) for p in plans) + 2
    for p in plans:
        extras = []
        if p.upsert:
            extras.append("UPSERT (singleton)")
        if p.renamed:
            extras.append("zmiany kolumn: " + ", ".join(f"{s}->{t}" for s, t in p.renamed.items()))
        if p.defaulted_tgt_cols:
            extras.append("default w celu: " + ", ".join(p.defaulted_tgt_cols))
        suffix = ("  [" + "; ".join(extras) + "]") if extras else ""
        print(f"  {p.label:<{w}} {p.src_count:>8} wierszy{suffix}")
    total = sum(p.src_count for p in plans)
    print(f"\n  Razem: {total} wierszy w {len(plans)} tabelach.")
    if truncate:
        print("  Tabele docelowe zostaną wyczyszczone (TRUNCATE ... CASCADE) przed kopią.")


# ---------------------------------------------------------------------------
# Kopiowanie
# ---------------------------------------------------------------------------

async def truncate_targets(tgt: asyncpg.Connection, plans: list[TablePlan]) -> None:
    # Jeden TRUNCATE na wszystkie tabele, od dzieci do rodziców + CASCADE.
    tables = ", ".join(p.tgt_qualified for p in reversed(plans))
    await tgt.execute(f"TRUNCATE {tables} CASCADE")
    print("  TRUNCATE tabel docelowych: OK")


async def copy_table(src: asyncpg.Connection, tgt: asyncpg.Connection, plan: TablePlan) -> int:
    select_sql = f"SELECT {', '.join(q(c) for c in plan.src_cols)} FROM {plan.src_qualified}"

    if plan.upsert:
        rows = await src.fetch(select_sql)
        col_list = ", ".join(q(c) for c in plan.tgt_cols)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(plan.tgt_cols)))
        set_clause = ", ".join(
            f"{q(c)} = EXCLUDED.{q(c)}" for c in plan.tgt_cols if c != "id"
        )
        conflict = f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING"
        sql = (
            f"INSERT INTO {plan.tgt_qualified} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) {conflict}"
        )
        for row in rows:
            await tgt.execute(sql, *row)
        return len(rows)

    copied = 0
    # Uwaga: bez prefetch= - asyncpg pozwala na prefetch tylko przy iteracji
    # `async for`; awaitowany kursor z prefetch rzuca InterfaceError.
    cursor = await src.cursor(select_sql)
    while True:
        batch = await cursor.fetch(BATCH_SIZE)
        if not batch:
            break
        # COPY binarny; wartości enumów i jsonb przechodzą jako stringi.
        await tgt.copy_records_to_table(
            plan.tgt_table,
            records=batch,
            columns=plan.tgt_cols,
            schema_name=plan.tgt_schema,
        )
        copied += len(batch)
    return copied


async def sync_sequences(tgt: asyncpg.Connection, plans: list[TablePlan]) -> list[str]:
    """setval na max(kolumny) dla każdej sekwencji serial/identity w tabelach docelowych.

    Tabele z PK uuid/text (np. admin.sessions) nie mają sekwencji - pg_get_serial_sequence
    zwraca NULL i są pomijane automatycznie.
    """
    fixed: list[str] = []
    for plan in plans:
        for col in plan.tgt_all_cols:
            seq = await tgt.fetchval(
                "SELECT pg_get_serial_sequence($1, $2)",
                f"{plan.tgt_schema}.{plan.tgt_table}",
                col,
            )
            if seq is None:
                continue
            max_val = await tgt.fetchval(f"SELECT max({q(col)}) FROM {plan.tgt_qualified}")
            if max_val is None:
                await tgt.execute("SELECT setval($1::regclass, 1, false)", seq)
                fixed.append(f"{seq} -> 1 (tabela pusta, is_called=false)")
            else:
                await tgt.execute("SELECT setval($1::regclass, $2, true)", seq, max_val)
                fixed.append(f"{seq} -> {max_val}")
    return fixed


# ---------------------------------------------------------------------------
# Weryfikacja
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    src_val: str
    tgt_val: str
    ok: bool


async def verify(
    src: asyncpg.Connection, tgt: asyncpg.Connection, plans: list[TablePlan]
) -> tuple[list[tuple[str, list[Check]]], bool]:
    results: list[tuple[str, list[Check]]] = []
    all_ok = True

    for plan in plans:
        checks: list[Check] = []
        s_t, t_t = plan.src_qualified, plan.tgt_qualified

        s_count = await src.fetchval(f"SELECT count(*) FROM {s_t}")
        t_count = await tgt.fetchval(f"SELECT count(*) FROM {t_t}")
        checks.append(Check("count", str(s_count), str(t_count), s_count == t_count))

        if plan.src_types.get("id") in NUMERIC_ID_TYPES:
            s_max = await src.fetchval(f"SELECT max(id) FROM {s_t}")
            t_max = await tgt.fetchval(f"SELECT max(id) FROM {t_t}")
            checks.append(Check("max(id)", str(s_max), str(t_max), s_max == t_max))

        if "status" in plan.src_cols:
            s_rows = await src.fetch(
                f"SELECT status::text AS s, count(*) AS c FROM {s_t} GROUP BY 1 ORDER BY 1"
            )
            t_rows = await tgt.fetch(
                f"SELECT status::text AS s, count(*) AS c FROM {t_t} GROUP BY 1 ORDER BY 1"
            )
            s_repr = ", ".join(f"{r['s']}={r['c']}" for r in s_rows) or "-"
            t_repr = ", ".join(f"{r['s']}={r['c']}" for r in t_rows) or "-"
            checks.append(Check("per status", s_repr, t_repr, s_repr == t_repr))

        if "created_at" in plan.src_cols:
            s_rng = await src.fetchrow(f"SELECT min(created_at) lo, max(created_at) hi FROM {s_t}")
            t_rng = await tgt.fetchrow(f"SELECT min(created_at) lo, max(created_at) hi FROM {t_t}")
            ok = s_rng["lo"] == t_rng["lo"] and s_rng["hi"] == t_rng["hi"]
            fmt = lambda r: f"{r['lo']} .. {r['hi']}" if r["lo"] is not None else "-"
            checks.append(Check("min/max created_at", fmt(s_rng), fmt(t_rng), ok))

        if not all(c.ok for c in checks):
            all_ok = False
        results.append((plan.label, checks))

    return results, all_ok


def print_report(results: list[tuple[str, list[Check]]]) -> None:
    print("\nWeryfikacja (stara baza vs nowa):\n")
    w_label = max(len(label) for label, _ in results) + 2
    w_name = max(len(c.name) for _, checks in results for c in checks) + 2
    w_val = 36
    header = f"  {'tabela':<{w_label}}{'test':<{w_name}}{'stara':<{w_val}}{'nowa':<{w_val}}wynik"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label, checks in results:
        for i, c in enumerate(checks):
            shown = label if i == 0 else ""
            mark = "OK" if c.ok else "ROZJAZD"
            print(f"  {shown:<{w_label}}{c.name:<{w_name}}{c.src_val:<{w_val}}{c.tgt_val:<{w_val}}{mark}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def confirm_truncate(target_dsn: str) -> bool:
    dbname = urlparse(target_dsn).path.lstrip("/") or "?"
    print(
        f"\nUWAGA: --truncate wyczyści (TRUNCATE ... CASCADE) wszystkie tabele "
        f"docelowe w bazie '{dbname}'."
    )
    try:
        answer = input(f"Wpisz nazwę bazy docelowej ('{dbname}') aby potwierdzić: ").strip()
    except EOFError:
        return False
    return answer == dbname


async def run(args: argparse.Namespace) -> int:
    print(f"Źródło: {mask_dsn(args.source_dsn)}")
    print(f"Cel:    {mask_dsn(args.target_dsn)}")

    src = await asyncpg.connect(args.source_dsn, server_settings={"application_name": "bfc-data-migration"})
    tgt = await asyncpg.connect(args.target_dsn, server_settings={"application_name": "bfc-data-migration"})
    try:
        # Spójny snapshot źródła na cały czas migracji i weryfikacji.
        async with src.transaction(isolation="repeatable_read", readonly=True):
            plans, errors = await build_plans(src, tgt)
            if errors:
                print("\nBŁĘDY planu migracji (nic nie zapisano):\n")
                for e in errors:
                    print(e)
                return 2

            print_plan(plans, truncate=args.truncate)

            if args.dry_run:
                print("\n--dry-run: nic nie zapisano.")
                return 0

            if args.truncate and not confirm_truncate(args.target_dsn):
                print("Brak potwierdzenia. Przerwano, nic nie zapisano.")
                return 2

            try:
                async with tgt.transaction():
                    print("\nKopiowanie:")
                    if args.truncate:
                        await truncate_targets(tgt, plans)
                    for plan in plans:
                        n = await copy_table(src, tgt, plan)
                        mode = "upsert" if plan.upsert else "copy"
                        print(f"  {plan.label}: {n} wierszy ({mode})")

                    print("\nSekwencje:")
                    for line in await sync_sequences(tgt, plans):
                        print(f"  {line}")

                    results, ok = await verify(src, tgt, plans)
                    print_report(results)
                    if not ok:
                        raise VerificationMismatch
            except VerificationMismatch:
                print(
                    "\nROZJAZD w weryfikacji. Transakcja docelowa WYCOFANA (rollback), "
                    "nowa baza bez zmian."
                )
                return 1

        print("\nOK. Migracja zakończona i zatwierdzona (COMMIT).")
        return 0
    finally:
        await src.close()
        await tgt.close()


def mask_dsn(dsn: str) -> str:
    import re

    return re.sub(r":[^:@/]+@", ":***@", dsn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migracja danych bfc_admin (public) -> befreeclub (admin, circle_dm)."
    )
    parser.add_argument("--source-dsn", default=DEFAULT_SOURCE_DSN, help=f"stara baza (default: {DEFAULT_SOURCE_DSN})")
    parser.add_argument("--target-dsn", default=DEFAULT_TARGET_DSN, help=f"nowa baza (default: {DEFAULT_TARGET_DSN})")
    parser.add_argument("--dry-run", action="store_true", help="pokaż plan i liczniki, nic nie zapisuj")
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="wyczyść tabele docelowe przed kopią (TRUNCATE CASCADE, wymaga potwierdzenia)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        code = asyncio.run(run(args))
    except (asyncpg.PostgresError, OSError) as exc:
        print(f"\nBŁĄD: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\nPrzerwano.", file=sys.stderr)
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
