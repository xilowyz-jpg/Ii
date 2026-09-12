"""OANDA v20 REST data source (practice account).

Requires `requests` and an OANDA practice token. Kept optional on purpose:
nothing else in the package imports it, so the backtest runs with no network
and no credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from fxagents.data.base import DataSource
from fxagents.types import Candle

HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}


class OandaConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class OandaSource(DataSource):
    token: str | None = None
    environment: str = "practice"
    price: str = "M"            # M(id), B(id), A(sk)
    timeout: float = 20.0

    def __post_init__(self) -> None:
        self.token = self.token or os.environ.get("OANDA_API_TOKEN")
        self.environment = (
            self.environment or os.environ.get("OANDA_ENV") or "practice"
        ).lower()
        if self.environment not in HOSTS:
            raise OandaConfigError(f"OANDA_ENV must be 'practice' or 'live', got {self.environment!r}")
        if self.environment == "live" and os.environ.get("FXAGENTS_ALLOW_LIVE") != "1":
            raise OandaConfigError(
                "refusing to connect to the live OANDA host. Set FXAGENTS_ALLOW_LIVE=1 "
                "only once you have deliberately decided to trade real money."
            )
        if not self.token:
            raise OandaConfigError(
                "no OANDA token. Put OANDA_API_TOKEN in your environment (see .env.example)."
            )

    @property
    def host(self) -> str:
        return HOSTS[self.environment]

    def candles(self, instrument: str, granularity: str = "H1", count: int = 500) -> list[Candle]:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise OandaConfigError(
                "the OANDA source needs `requests`: pip install 'fxagents[live]'"
            ) from exc

        symbol = instrument.upper().replace("/", "_")
        out: list[Candle] = []
        remaining = count
        to_time: str | None = None

        # v20 caps a single request at 5000 candles; page backwards if needed.
        while remaining > 0:
            batch = min(remaining, 5000)
            params = {
                "granularity": granularity.upper(),
                "price": self.price,
                "count": batch,
            }
            if to_time:
                params["to"] = to_time
            resp = requests.get(
                f"{self.host}/v3/instruments/{symbol}/candles",
                headers={"Authorization": f"Bearer {self.token}"},
                params=params,
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise OandaConfigError(
                    f"OANDA returned {resp.status_code} for {symbol}: {resp.text[:300]}"
                )
            raw = resp.json().get("candles", [])
            if not raw:
                break

            page = [self._to_candle(c) for c in raw if c.get("complete")]
            out = page + out
            remaining -= len(raw)
            to_time = raw[0]["time"]
            if len(raw) < batch:
                break

        out.sort(key=lambda c: c.ts)
        # Deduplicate on timestamp, which paging can produce at the seams.
        seen: set[datetime] = set()
        deduped: list[Candle] = []
        for c in out:
            if c.ts not in seen:
                seen.add(c.ts)
                deduped.append(c)
        return deduped[-count:]

    def _to_candle(self, raw: dict) -> Candle:
        ohlc = raw.get("mid") or raw.get("bid") or raw.get("ask")
        ts = datetime.fromisoformat(raw["time"].replace("Z", "+00:00"))
        return Candle(
            ts=ts.astimezone(timezone.utc),
            open=float(ohlc["o"]),
            high=float(ohlc["h"]),
            low=float(ohlc["l"]),
            close=float(ohlc["c"]),
            volume=float(raw.get("volume", 0)),
        )
