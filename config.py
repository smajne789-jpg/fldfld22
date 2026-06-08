from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default).strip().replace(",", "."))


def _parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        result.add(int(chunk))
    return result


@dataclass(frozen=True)
class Config:
    bot_token: str
    crypto_pay_token: str
    crypto_pay_base_url: str
    admin_ids: set[int]
    bot_username: str | None
    db_path: Path
    default_reserve_usd: Decimal
    accepted_assets: str
    payout_asset: str
    dice_animation_delay: float
    invoice_poll_seconds: int
    min_deposit_usd: Decimal = Decimal("0.30")
    min_withdraw_usd: Decimal = Decimal("1.70")
    min_required_deposits_usd: Decimal = Decimal("1.00")
    min_bet_usd: Decimal = Decimal("0.10")


def load_config() -> Config:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    crypto_pay_token = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required in environment")
    if not crypto_pay_token:
        raise RuntimeError("CRYPTO_PAY_TOKEN is required in environment")

    testnet = _env_bool("CRYPTO_PAY_TESTNET", False)
    default_base_url = "https://testnet-pay.crypt.bot/api" if testnet else "https://pay.crypt.bot/api"

    return Config(
        bot_token=bot_token,
        crypto_pay_token=crypto_pay_token,
        crypto_pay_base_url=os.getenv("CRYPTO_PAY_API_URL", default_base_url).rstrip("/"),
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        bot_username=os.getenv("BOT_USERNAME", "").strip().lstrip("@") or None,
        db_path=Path(os.getenv("DB_PATH", "data/bot.sqlite3")),
        default_reserve_usd=_env_decimal("DEFAULT_RESERVE_USD", "100.00"),
        accepted_assets=os.getenv("ACCEPTED_ASSETS", "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC").strip(),
        payout_asset=os.getenv("PAYOUT_ASSET", "USDT").strip().upper(),
        dice_animation_delay=float(os.getenv("DICE_ANIMATION_DELAY", "2.4")),
        invoice_poll_seconds=int(os.getenv("INVOICE_POLL_SECONDS", "20")),
    )
