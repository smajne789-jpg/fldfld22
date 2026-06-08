from __future__ import annotations

from decimal import Decimal
from typing import Any

import aiohttp


def amount_to_api(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class CryptoPayError(RuntimeError):
    pass


class CryptoPayClient:
    def __init__(self, token: str, base_url: str) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Crypto-Pay-API-Token": self._token},
                timeout=aiohttp.ClientTimeout(total=25),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        session = await self._get_session()
        url = f"{self._base_url}/{method}"
        async with session.post(url, json=payload or {}) as response:
            try:
                data = await response.json(content_type=None)
            except Exception as exc:
                body = await response.text()
                raise CryptoPayError(f"Crypto Pay returned non-JSON response: {body[:300]}") from exc

        if not data.get("ok"):
            error = data.get("error") or data
            raise CryptoPayError(f"Crypto Pay error in {method}: {error}")
        return data.get("result")

    async def create_invoice_usd(
        self,
        *,
        amount: Decimal,
        user_id: int,
        accepted_assets: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": "USD",
            "amount": amount_to_api(amount),
            "description": f"Deposit for Telegram user {user_id}",
            "payload": f"deposit:{user_id}",
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 3600,
        }
        if accepted_assets:
            payload["accepted_assets"] = accepted_assets
        return await self.request("createInvoice", payload)

    async def get_invoices(self, invoice_ids: list[int]) -> list[dict[str, Any]]:
        result = await self.request(
            "getInvoices",
            {"invoice_ids": ",".join(str(invoice_id) for invoice_id in invoice_ids)},
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            return result["items"]
        if isinstance(result, dict) and "invoice_id" in result:
            return [result]
        return []

    async def transfer(
        self,
        *,
        user_id: int,
        asset: str,
        amount: Decimal,
        spend_id: str,
        comment: str,
    ) -> dict[str, Any]:
        return await self.request(
            "transfer",
            {
                "user_id": user_id,
                "asset": asset,
                "amount": amount_to_api(amount),
                "spend_id": spend_id,
                "comment": comment[:1024],
                "disable_send_notification": False,
            },
        )
