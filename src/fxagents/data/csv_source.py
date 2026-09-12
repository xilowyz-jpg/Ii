"""CSV loader for real historical data.

Accepts the column layouts the common free sources ship with (Dukascopy,
HistData, OANDA exports, MetaTrader) by matching header names case-insensitively
rather than insisting on a fixed order.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fxagents.data.base import DataSource
from fxagents.types import Candle

_ALIASES: dict[str, tuple[str, ...]] = {
    "ts": ("time", "timestamp", "date", "datetime", "gmt time", "local time"),
    "open": ("open", "o", "bidopen", "openbid"),
    "high": ("high", "h", "bidhigh", "highbid"),
    "low": ("low", "l", "bidlow", "lowbid"),
    "close": ("close", "c", "bidclose", "closebid"),
    "volume": ("volume", "vol", "v", "tickvolume", "tick volume"),
}

_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S.%f",
    "%d.%m.%Y %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%m/%d/%Y %H:%M",
)


def _parse_time(raw: str) -> datetime:
    text = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _TIME_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unrecognised timestamp format: {raw!r}")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_columns(header: list[str]) -> dict[str, int]:
    normalised = [h.strip().lower().lstrip("﻿") for h in header]
    mapping: dict[str, int] = {}
    for field, aliases in _ALIASES.items():
        for idx, name in enumerate(normalised):
            if name in aliases:
                mapping[field] = idx
                break
    missing = {"ts", "open", "high", "low", "close"} - mapping.keys()
    if missing:
        raise ValueError(
            f"CSV is missing required column(s) {sorted(missing)}; header was {header}"
        )
    return mapping


@dataclass(slots=True)
class CsvSource(DataSource):
    """Reads `<directory>/<INSTRUMENT>_<GRANULARITY>.csv`, or an explicit path."""

    directory: Path | str = "data"
    path: Path | str | None = None

    def _file_for(self, instrument: str, granularity: str) -> Path:
        if self.path is not None:
            return Path(self.path)
        return Path(self.directory) / f"{instrument.upper()}_{granularity.upper()}.csv"

    def candles(self, instrument: str, granularity: str = "H1", count: int = 5000) -> list[Candle]:
        file = self._file_for(instrument, granularity)
        if not file.exists():
            raise FileNotFoundError(
                f"no data file at {file}. Point CsvSource at your own export, or use "
                "the synthetic source with --source synthetic."
            )

        rows: list[Candle] = []
        with file.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return []
            cols = _resolve_columns(header)
            vol_idx = cols.get("volume")
            for line in reader:
                if not line or not line[cols["ts"]].strip():
                    continue
                try:
                    rows.append(
                        Candle(
                            ts=_parse_time(line[cols["ts"]]),
                            open=float(line[cols["open"]]),
                            high=float(line[cols["high"]]),
                            low=float(line[cols["low"]]),
                            close=float(line[cols["close"]]),
                            volume=float(line[vol_idx]) if vol_idx is not None and line[vol_idx] else 0.0,
                        )
                    )
                except (ValueError, IndexError) as exc:
                    raise ValueError(f"bad row in {file}: {line} ({exc})") from exc

        rows.sort(key=lambda c: c.ts)
        return rows[-count:] if count else rows
