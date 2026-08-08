import os
from datetime import date, time

import psycopg
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from .env")


def get_connection():
    return psycopg.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=10,
    )


def init_db() -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    profile JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE,
                    role TEXT NOT NULL
                        CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_messages_user_created
                ON messages (user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS watchlist (
                    user_id BIGINT NOT NULL
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS briefing_preferences (
                    user_id BIGINT PRIMARY KEY
                        REFERENCES users(telegram_id)
                        ON DELETE CASCADE,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    briefing_time TIME NOT NULL DEFAULT '08:00',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                    last_sent_date DATE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )


def upsert_user(
    telegram_id: int,
    first_name: str | None,
    username: str | None,
) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (
                    telegram_id,
                    first_name,
                    username
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id)
                DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    username = EXCLUDED.username,
                    updated_at = NOW()
                """,
                (
                    telegram_id,
                    first_name,
                    username,
                ),
            )


def save_message(
    user_id: int,
    role: str,
    content: str,
) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO messages (
                    user_id,
                    role,
                    content
                )
                VALUES (%s, %s, %s)
                """,
                (
                    user_id,
                    role,
                    content,
                ),
            )


def get_recent_messages(
    user_id: int,
    limit: int = 10,
) -> list[tuple[str, str]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT role, content
                FROM (
                    SELECT id, role, content
                    FROM messages
                    WHERE user_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) AS recent
                ORDER BY id ASC
                """,
                (
                    user_id,
                    limit,
                ),
            )

            return cursor.fetchall()


def add_to_watchlist(
    user_id: int,
    symbol: str,
) -> None:
    normalized_symbol = symbol.upper().strip()

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO watchlist (
                    user_id,
                    symbol
                )
                VALUES (%s, %s)
                ON CONFLICT (user_id, symbol)
                DO NOTHING
                """,
                (
                    user_id,
                    normalized_symbol,
                ),
            )


def remove_from_watchlist(
    user_id: int,
    symbol: str,
) -> bool:
    normalized_symbol = symbol.upper().strip()

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM watchlist
                WHERE user_id = %s
                AND symbol = %s
                """,
                (
                    user_id,
                    normalized_symbol,
                ),
            )

            return cursor.rowcount > 0


def get_watchlist(
    user_id: int,
) -> list[str]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT symbol
                FROM watchlist
                WHERE user_id = %s
                ORDER BY created_at ASC
                """,
                (user_id,),
            )

            return [
                row[0]
                for row in cursor.fetchall()
            ]


def set_briefing_preferences(
    user_id: int,
    enabled: bool,
    briefing_time: time,
    timezone_name: str = "Asia/Kolkata",
) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO briefing_preferences (
                    user_id,
                    enabled,
                    briefing_time,
                    timezone
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    briefing_time = EXCLUDED.briefing_time,
                    timezone = EXCLUDED.timezone,
                    updated_at = NOW()
                """,
                (
                    user_id,
                    enabled,
                    briefing_time,
                    timezone_name,
                ),
            )


def get_briefing_preferences(
    user_id: int,
) -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    enabled,
                    briefing_time,
                    timezone,
                    last_sent_date
                FROM briefing_preferences
                WHERE user_id = %s
                """,
                (user_id,),
            )

            row = cursor.fetchone()

            if not row:
                return None

            return {
                "enabled": row[0],
                "briefing_time": row[1],
                "timezone": row[2],
                "last_sent_date": row[3],
            }


def get_enabled_briefings() -> list[dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    preferences.user_id,
                    preferences.briefing_time,
                    preferences.timezone,
                    preferences.last_sent_date,
                    watchlist.symbol
                FROM briefing_preferences AS preferences
                LEFT JOIN watchlist
                    ON watchlist.user_id = preferences.user_id
                WHERE preferences.enabled = TRUE
                ORDER BY preferences.user_id, watchlist.created_at
                """
            )

            rows = cursor.fetchall()

    users: dict[int, dict] = {}

    for (
        user_id,
        briefing_time,
        timezone_name,
        last_sent_date,
        symbol,
    ) in rows:
        if user_id not in users:
            users[user_id] = {
                "user_id": user_id,
                "briefing_time": briefing_time,
                "timezone": timezone_name,
                "last_sent_date": last_sent_date,
                "symbols": [],
            }

        if symbol:
            users[user_id]["symbols"].append(symbol)

    return list(users.values())


def mark_briefing_sent(
    user_id: int,
    sent_date: date,
) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE briefing_preferences
                SET
                    last_sent_date = %s,
                    updated_at = NOW()
                WHERE user_id = %s
                """,
                (
                    sent_date,
                    user_id,
                ),
            )