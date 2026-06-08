from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dec(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def money_text(value: Decimal | str | int | float) -> str:
    amount = dec(value)
    text = format(amount.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


@dataclass(frozen=True)
class WithdrawalHold:
    id: int
    user_id: int
    username: str | None
    amount: Decimal
    spend_id: str


class Database:
    def __init__(self, path: Path, default_reserve: Decimal) -> None:
        self.path = path
        self.default_reserve = default_reserve
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not initialized")
        return self._conn

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        async with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance TEXT NOT NULL DEFAULT '0',
                    total_deposits TEXT NOT NULL DEFAULT '0',
                    total_withdrawals TEXT NOT NULL DEFAULT '0',
                    games_played INTEGER NOT NULL DEFAULT 0,
                    games_won INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pay_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    paid_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL,
                    spend_id TEXT NOT NULL UNIQUE,
                    transfer_id TEXT,
                    fail_reason TEXT,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    admin_id INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS promo_checks (
                    code TEXT PRIMARY KEY,
                    amount TEXT NOT NULL,
                    max_activations INTEGER NOT NULL,
                    activations INTEGER NOT NULL DEFAULT 0,
                    min_deposits TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS check_claims (
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (code, user_id),
                    FOREIGN KEY (code) REFERENCES promo_checks(code),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    balance_after TEXT NOT NULL,
                    meta TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('reserve_usd', ?)",
                (money_text(self.default_reserve),),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('auto_withdrawals', '1')"
            )
            self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        now = utc_now()
        async with self._lock:
            self.conn.execute(
                """
                INSERT INTO users(user_id, username, first_name, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username, first_name, now, now),
            )
            self.conn.commit()

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return row_to_dict(row)

    async def get_reserve(self) -> Decimal:
        async with self._lock:
            return self._get_reserve_unlocked()

    async def set_reserve(self, amount: Decimal) -> None:
        async with self._lock:
            self._set_setting_unlocked("reserve_usd", money_text(amount))
            self.conn.commit()

    async def change_reserve(self, delta: Decimal) -> Decimal:
        async with self._lock:
            current = self._get_reserve_unlocked()
            new_value = current + delta
            if new_value < 0:
                raise ValueError("Reserve cannot be negative")
            self._set_setting_unlocked("reserve_usd", money_text(new_value))
            self.conn.commit()
            return new_value

    async def get_auto_withdrawals(self) -> bool:
        async with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = 'auto_withdrawals'"
            ).fetchone()
            return (row["value"] if row else "1") == "1"

    async def set_auto_withdrawals(self, enabled: bool) -> None:
        async with self._lock:
            self._set_setting_unlocked("auto_withdrawals", "1" if enabled else "0")
            self.conn.commit()

    async def create_invoice(
        self,
        *,
        invoice_id: int,
        user_id: int,
        amount: Decimal,
        pay_url: str,
    ) -> None:
        async with self._lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO invoices(invoice_id, user_id, amount, status, pay_url, created_at)
                VALUES(?, ?, ?, 'pending', ?, ?)
                """,
                (invoice_id, user_id, money_text(amount), pay_url, utc_now()),
            )
            self.conn.commit()

    async def get_invoice(self, invoice_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = self.conn.execute(
                "SELECT * FROM invoices WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchone()
            return row_to_dict(row)

    async def list_pending_invoices(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM invoices
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [row_to_dict(row) for row in rows if row is not None]

    async def mark_invoice_paid(self, invoice_id: int) -> dict[str, Any] | None:
        async with self._lock:
            invoice = self.conn.execute(
                "SELECT * FROM invoices WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchone()
            if invoice is None:
                return None
            if invoice["status"] == "paid":
                return row_to_dict(invoice)

            user = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (invoice["user_id"],),
            ).fetchone()
            if user is None:
                return None

            amount = dec(invoice["amount"])
            new_balance = dec(user["balance"]) + amount
            total_deposits = dec(user["total_deposits"]) + amount
            self.conn.execute(
                """
                UPDATE users
                SET balance = ?, total_deposits = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (money_text(new_balance), money_text(total_deposits), utc_now(), user["user_id"]),
            )
            self.conn.execute(
                "UPDATE invoices SET status = 'paid', paid_at = ? WHERE invoice_id = ?",
                (utc_now(), invoice_id),
            )
            self._change_reserve_unlocked(amount)
            self._add_transaction_unlocked(
                user["user_id"],
                "deposit",
                amount,
                new_balance,
                {"invoice_id": invoice_id},
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT * FROM invoices WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchone()
            return row_to_dict(updated)

    async def create_withdrawal_hold(
        self,
        *,
        user_id: int,
        username: str | None,
        amount: Decimal,
    ) -> WithdrawalHold:
        async with self._lock:
            user = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                raise ValueError("User not found")

            balance = dec(user["balance"])
            if balance < amount:
                raise ValueError("Insufficient balance")

            reserve = self._get_reserve_unlocked()
            if reserve < amount:
                raise ValueError("Insufficient reserve")

            spend_id = f"wd:{user_id}:{secrets.token_hex(10)}"
            new_balance = balance - amount
            self.conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE user_id = ?",
                (money_text(new_balance), utc_now(), user_id),
            )
            self._set_setting_unlocked("reserve_usd", money_text(reserve - amount))
            self.conn.execute(
                """
                INSERT INTO withdrawals(user_id, username, amount, status, spend_id, created_at)
                VALUES(?, ?, ?, 'pending', ?, ?)
                """,
                (user_id, username, money_text(amount), spend_id, utc_now()),
            )
            withdrawal_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._add_transaction_unlocked(
                user_id,
                "withdraw_hold",
                -amount,
                new_balance,
                {"withdrawal_id": withdrawal_id},
            )
            self.conn.commit()
            return WithdrawalHold(
                id=int(withdrawal_id),
                user_id=user_id,
                username=username,
                amount=amount,
                spend_id=spend_id,
            )

    async def get_withdrawal(self, withdrawal_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = self.conn.execute(
                "SELECT * FROM withdrawals WHERE id = ?",
                (withdrawal_id,),
            ).fetchone()
            return row_to_dict(row)

    async def set_withdrawal_fail_reason(self, withdrawal_id: int, reason: str) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE withdrawals SET fail_reason = ? WHERE id = ? AND status = 'pending'",
                (reason[:900], withdrawal_id),
            )
            self.conn.commit()

    async def mark_withdrawal_paid(
        self,
        *,
        withdrawal_id: int,
        transfer_id: str | None,
        admin_id: int | None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = self.conn.execute(
                "SELECT * FROM withdrawals WHERE id = ?",
                (withdrawal_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "paid":
                return row_to_dict(row)
            if row["status"] != "pending":
                raise ValueError("Withdrawal is not pending")

            user = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (row["user_id"],),
            ).fetchone()
            if user is None:
                return None

            amount = dec(row["amount"])
            total_withdrawals = dec(user["total_withdrawals"]) + amount
            self.conn.execute(
                """
                UPDATE users
                SET total_withdrawals = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (money_text(total_withdrawals), utc_now(), row["user_id"]),
            )
            self.conn.execute(
                """
                UPDATE withdrawals
                SET status = 'paid', transfer_id = ?, admin_id = ?, processed_at = ?
                WHERE id = ?
                """,
                (transfer_id, admin_id, utc_now(), withdrawal_id),
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT * FROM withdrawals WHERE id = ?",
                (withdrawal_id,),
            ).fetchone()
            return row_to_dict(updated)

    async def decline_withdrawal(self, *, withdrawal_id: int, admin_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = self.conn.execute(
                "SELECT * FROM withdrawals WHERE id = ?",
                (withdrawal_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "declined":
                return row_to_dict(row)
            if row["status"] != "pending":
                raise ValueError("Withdrawal is not pending")

            user = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (row["user_id"],),
            ).fetchone()
            if user is None:
                return None

            amount = dec(row["amount"])
            new_balance = dec(user["balance"]) + amount
            self.conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE user_id = ?",
                (money_text(new_balance), utc_now(), row["user_id"]),
            )
            self._change_reserve_unlocked(amount)
            self.conn.execute(
                """
                UPDATE withdrawals
                SET status = 'declined', admin_id = ?, processed_at = ?
                WHERE id = ?
                """,
                (admin_id, utc_now(), withdrawal_id),
            )
            self._add_transaction_unlocked(
                row["user_id"],
                "withdraw_refund",
                amount,
                new_balance,
                {"withdrawal_id": withdrawal_id},
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT * FROM withdrawals WHERE id = ?",
                (withdrawal_id,),
            ).fetchone()
            return row_to_dict(updated)

    async def settle_game(
        self,
        *,
        user_id: int,
        game_code: str,
        stake: Decimal,
        payout: Decimal,
        rolls: list[int],
    ) -> dict[str, Any]:
        async with self._lock:
            user = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                raise ValueError("User not found")

            balance = dec(user["balance"])
            if balance < stake:
                raise ValueError("Insufficient balance")

            won = payout > 0
            net_reserve_delta = stake if not won else -(payout - stake)
            reserve = self._get_reserve_unlocked()
            if reserve + net_reserve_delta < 0:
                raise ValueError("Insufficient reserve")

            new_balance = balance - stake + payout
            self.conn.execute(
                """
                UPDATE users
                SET balance = ?,
                    games_played = games_played + 1,
                    games_won = games_won + ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (money_text(new_balance), 1 if won else 0, utc_now(), user_id),
            )
            self._set_setting_unlocked("reserve_usd", money_text(reserve + net_reserve_delta))
            self._add_transaction_unlocked(
                user_id,
                "game_win" if won else "game_loss",
                payout - stake,
                new_balance,
                {"game": game_code, "stake": money_text(stake), "payout": money_text(payout), "rolls": rolls},
            )
            self.conn.commit()
            return {
                "balance": new_balance,
                "reserve": reserve + net_reserve_delta,
                "won": won,
            }

    async def create_promo_check(
        self,
        *,
        amount: Decimal,
        max_activations: int,
        min_deposits: Decimal,
        created_by: int,
    ) -> str:
        async with self._lock:
            while True:
                code = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
                exists = self.conn.execute(
                    "SELECT 1 FROM promo_checks WHERE code = ?",
                    (code,),
                ).fetchone()
                if exists is None:
                    break
            self.conn.execute(
                """
                INSERT INTO promo_checks(code, amount, max_activations, min_deposits, created_by, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (code, money_text(amount), max_activations, money_text(min_deposits), created_by, utc_now()),
            )
            self.conn.commit()
            return code

    async def claim_promo_check(self, *, code: str, user_id: int) -> tuple[bool, str, Decimal | None]:
        async with self._lock:
            check = self.conn.execute(
                "SELECT * FROM promo_checks WHERE code = ?",
                (code,),
            ).fetchone()
            if check is None:
                return False, "Чек не найден или ссылка устарела.", None
            if check["activations"] >= check["max_activations"]:
                return False, "У этого чека уже закончились активации.", None

            claimed = self.conn.execute(
                "SELECT 1 FROM check_claims WHERE code = ? AND user_id = ?",
                (code, user_id),
            ).fetchone()
            if claimed is not None:
                return False, "Вы уже активировали этот чек.", None

            user = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                return False, "Профиль не найден. Нажмите /start еще раз.", None

            min_deposits = dec(check["min_deposits"])
            if min_deposits > 0 and dec(user["total_deposits"]) < min_deposits:
                return (
                    False,
                    f"Для этого чека нужно иметь депозитов от {money_text(min_deposits)} $.",
                    None,
                )

            amount = dec(check["amount"])
            new_balance = dec(user["balance"]) + amount
            self.conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE user_id = ?",
                (money_text(new_balance), utc_now(), user_id),
            )
            self.conn.execute(
                "UPDATE promo_checks SET activations = activations + 1 WHERE code = ?",
                (code,),
            )
            self.conn.execute(
                "INSERT INTO check_claims(code, user_id, claimed_at) VALUES(?, ?, ?)",
                (code, user_id, utc_now()),
            )
            self._add_transaction_unlocked(
                user_id,
                "promo_check",
                amount,
                new_balance,
                {"code": code},
            )
            self.conn.commit()
            return True, "Чек активирован.", amount

    async def admin_stats(self) -> dict[str, Any]:
        async with self._lock:
            users = self.conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
            pending_withdrawals = self.conn.execute(
                "SELECT COUNT(*) AS count FROM withdrawals WHERE status = 'pending'"
            ).fetchone()["count"]
            sums = self.conn.execute(
                """
                SELECT
                    COALESCE(SUM(CAST(balance AS REAL)), 0) AS balances,
                    COALESCE(SUM(CAST(total_deposits AS REAL)), 0) AS deposits,
                    COALESCE(SUM(CAST(total_withdrawals AS REAL)), 0) AS withdrawals
                FROM users
                """
            ).fetchone()
            return {
                "users": users,
                "pending_withdrawals": pending_withdrawals,
                "balances": Decimal(str(sums["balances"])),
                "deposits": Decimal(str(sums["deposits"])),
                "withdrawals": Decimal(str(sums["withdrawals"])),
                "reserve": self._get_reserve_unlocked(),
            }

    def _get_reserve_unlocked(self) -> Decimal:
        row = self.conn.execute("SELECT value FROM settings WHERE key = 'reserve_usd'").fetchone()
        return dec(row["value"] if row else self.default_reserve)

    def _set_setting_unlocked(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _change_reserve_unlocked(self, delta: Decimal) -> None:
        new_value = self._get_reserve_unlocked() + delta
        if new_value < 0:
            raise ValueError("Reserve cannot be negative")
        self._set_setting_unlocked("reserve_usd", money_text(new_value))

    def _add_transaction_unlocked(
        self,
        user_id: int,
        tx_type: str,
        amount: Decimal,
        balance_after: Decimal,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO transactions(user_id, type, amount, balance_after, meta, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                tx_type,
                money_text(amount),
                money_text(balance_after),
                json.dumps(meta or {}, ensure_ascii=False),
                utc_now(),
            ),
        )
