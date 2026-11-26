"""Synthetic Trade Republic transaction export (file schema v1).

Every instrument, person and amount here is invented. The generator mirrors the
format conventions of real exports (column set, quoting, sign rules, two timestamp
precisions, local booking date vs UTC timestamp) so the parser is exercised on
realistic input. Ground truth (holdings, row counts) comes from the generator's own
bookkeeping, never from parsing its output.
"""

import csv
import hashlib
import io
import random
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from our_pocket.etl.transform.trade_republic import COLUMNS_V1
from our_pocket.etl.transform.validators import isin_check_digit

BERLIN = ZoneInfo("Europe/Berlin")
# Real exports book these a day before the Berlin date of their timestamp (D-028).
BOOKED_A_DAY_EARLY = frozenset({"TAX_OPTIMIZATION"})
CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
QTY = Decimal("0.0000000001")
TAX_RATE = Decimal("0.26375")  # Abgeltungsteuer + Soli

OWNER = "Alex Example"
OWNER_IBAN = "DE89370400440532013000"  # the standard documentation example IBAN


@dataclass(frozen=True)
class Instrument:
    name: str
    country: str
    asset_class: str
    start_price: Decimal
    dividend_currency: str = "EUR"
    dividend_per_share: Decimal | None = None

    @property
    def isin(self) -> str:
        body = self.country + hashlib.sha256(self.name.encode()).hexdigest().upper()[:9]
        return body + isin_check_digit(body)


WORLD_ETF = Instrument("Synthetic World Equity UCITS ETF (Acc)", "IE", "FUND", Decimal("95"))
DIVIDEND_ETF = Instrument(
    "Fictional Global Dividend UCITS ETF (Dist)",
    "IE",
    "FUND",
    Decimal("30"),
    dividend_per_share=Decimal("0.18"),
)
US_STOCK = Instrument(
    "Example Robotics Inc.",
    "US",
    "STOCK",
    Decimal("140"),
    dividend_currency="USD",
    dividend_per_share=Decimal("0.42"),
)
DE_STOCK = Instrument(
    "Placeholder Versorgung AG", "DE", "STOCK", Decimal("25"), dividend_per_share=Decimal("0.90")
)
INSTRUMENTS = (WORLD_ETF, DIVIDEND_ETF, US_STOCK, DE_STOCK)


@dataclass
class SyntheticExport:
    rows: list[dict[str, str]]
    holdings: dict[str, Decimal]
    type_counts: Counter[str] = field(default_factory=Counter)
    # Cash the generator moved: the balance the app would show.
    cash: Decimal = Decimal(0)

    def to_csv_bytes(self) -> bytes:
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=COLUMNS_V1, quoting=csv.QUOTE_ALL, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(self.rows)
        return buf.getvalue().encode()

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_csv_bytes())


def _format_decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


class _Generator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.rows: list[tuple[datetime, dict[str, str]]] = []
        self.holdings: defaultdict[str, Decimal] = defaultdict(Decimal)
        self.cash = Decimal(0)
        self.prices = {i.isin: i.start_price for i in INSTRUMENTS}

    # --- primitives ---------------------------------------------------------------

    def _timestamp(self, day: date) -> datetime:
        # Between 06:00 and 22:59 UTC: late trades land on the next local day in summer.
        seconds = self.rng.randrange(6 * 3600, 23 * 3600)
        micros = self.rng.randrange(1_000_000)
        return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(
            seconds=seconds, microseconds=micros
        )

    def _emit(self, day: date, tr_type: str, **fields: Decimal | str | None) -> None:
        ts = self._timestamp(day)
        lag = 1 if tr_type in BOOKED_A_DAY_EARLY else 0
        # Real exports mix microsecond and millisecond precision.
        stamp = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")
        if self.rng.random() < 0.5:
            stamp = stamp[:-3]
        row = dict.fromkeys(COLUMNS_V1, "")
        row.update(
            datetime=stamp + "Z",
            date=(ts.astimezone(BERLIN).date() - timedelta(days=lag)).isoformat(),
            account_type="DEFAULT",
            category="TRADING" if tr_type in ("BUY", "SELL") else "CASH",
            type=tr_type,
            currency="EUR",
            transaction_id=str(uuid.UUID(int=self.rng.getrandbits(128), version=4)),
        )
        for key, value in fields.items():
            row[key] = value if isinstance(value, str) else _format_decimal(value)
        for key in ("amount", "fee", "tax"):
            value = fields.get(key)
            if isinstance(value, Decimal):
                self.cash += value
        self.rows.append((ts, row))

    def _instrument_fields(self, instrument: Instrument) -> dict[str, str]:
        return {
            "asset_class": instrument.asset_class,
            "name": instrument.name,
            "symbol": instrument.isin,
        }

    def _tax(self, base: Decimal) -> Decimal:
        return -(base * TAX_RATE).quantize(CENT)

    # --- events -------------------------------------------------------------------

    def deposit(self, day: date, amount: Decimal, *, transfer: bool = False) -> None:
        if transfer:
            self._emit(
                day,
                "TRANSFER_INBOUND",
                amount=amount,
                name=OWNER,
                description=f"Incoming transfer from {OWNER}",
                counterparty_name=OWNER,
                counterparty_iban=OWNER_IBAN,
            )
        else:
            self._emit(
                day,
                "CUSTOMER_INBOUND",
                amount=amount,
                name=OWNER,
                description="Trade Republic",
                counterparty_name=OWNER,
                counterparty_iban=OWNER_IBAN,
            )

    def withdraw(self, day: date, amount: Decimal) -> None:
        self._emit(
            day,
            "CUSTOMER_OUTBOUND_REQUEST",
            amount=-amount,
            name=OWNER,
            description="Payout",
            counterparty_name=OWNER,
            counterparty_iban=OWNER_IBAN,
        )

    def buy(
        self,
        day: date,
        instrument: Instrument,
        *,
        spend: Decimal | None = None,
        shares: Decimal | None = None,
    ) -> None:
        price = self.prices[instrument.isin]
        savings_plan = spend is not None
        if spend is not None:
            shares = (spend / price).quantize(QTY)
        assert shares is not None
        self.holdings[instrument.isin] += shares
        description = (
            f"Savings plan execution {instrument.isin} {instrument.name}"
            if savings_plan
            else f"Buy trade {instrument.isin} {instrument.name}, "
            f"quantity: {_format_decimal(shares)}"
        )
        self._emit(
            day,
            "BUY",
            **self._instrument_fields(instrument),
            shares=shares,
            price=price,
            amount=-(shares * price).quantize(CENT),
            fee=None if savings_plan else Decimal("-1.00"),
            description=description,
        )

    def sell(self, day: date, instrument: Instrument, shares: Decimal) -> None:
        price = self.prices[instrument.isin]
        self.holdings[instrument.isin] -= shares
        amount = (shares * price).quantize(CENT)
        self._emit(
            day,
            "SELL",
            **self._instrument_fields(instrument),
            shares=-shares,
            price=price,
            amount=amount,
            fee=Decimal("-1.00"),
            tax=self._tax(amount * Decimal("0.2")),
            description=f"Sell trade {instrument.isin} {instrument.name}, "
            f"quantity: {_format_decimal(shares)}",
        )

    def dividend(self, day: date, instrument: Instrument) -> None:
        shares = self.holdings[instrument.isin]
        if shares <= 0 or instrument.dividend_per_share is None:
            return
        fields: dict[str, Decimal | str | None] = {
            **self._instrument_fields(instrument),
            "shares": shares,
        }
        if instrument.dividend_currency == "EUR":
            amount = (shares * instrument.dividend_per_share).quantize(MICRO)
        else:
            original = (shares * instrument.dividend_per_share).quantize(CENT)
            fx_rate = Decimal(str(self.rng.uniform(0.86, 0.95))).quantize(MICRO)
            amount = (original * fx_rate).quantize(MICRO)
            fields.update(
                original_amount=original,
                original_currency=instrument.dividend_currency,
                fx_rate=fx_rate,
            )
        tr_type = "DISTRIBUTION" if instrument.asset_class == "FUND" else "DIVIDEND"
        if tr_type == "DIVIDEND":
            fields.update(
                tax=self._tax(amount), description=f"Cash Dividend for ISIN {instrument.isin}"
            )
        self._emit(day, tr_type, amount=amount, **fields)

    def interest(self, day: date, balance: Decimal) -> None:
        amount = (balance * Decimal("0.02") / 12).quantize(CENT)
        if amount > 0:
            self._emit(
                day,
                "INTEREST_PAYMENT",
                amount=amount,
                tax=self._tax(amount),
                description="Interest payment Booking",
            )

    def vorabpauschale(self, day: date, instrument: Instrument) -> None:
        base = (
            self.holdings[instrument.isin] * self.prices[instrument.isin] * Decimal("0.007")
        ).quantize(CENT)
        if base > 0:
            self._emit(
                day,
                "EARNINGS",
                **self._instrument_fields(instrument),
                amount=Decimal("0.000000"),
                tax=self._tax(base * Decimal("0.7")),
                description=f"Vorabpauschale for ISIN {instrument.isin}",
            )

    def tax_optimisation(self, day: date) -> None:
        refund = Decimal(self.rng.randrange(50, 2000)) / 100
        self._emit(
            day,
            "TAX_OPTIMIZATION",
            amount=Decimal("0.000000"),
            tax=refund,
            description="Tax Optimisation",
        )

    def stock_perk(self, day: date, instrument: Instrument, amount: Decimal) -> None:
        self._emit(
            day,
            "STOCKPERK",
            **self._instrument_fields(instrument),
            amount=amount,
            description=f"Stock perk {instrument.isin}",
        )

    def move_prices(self) -> None:
        for isin, price in self.prices.items():
            drift = Decimal(str(self.rng.gauss(0.006, 0.045)))
            self.prices[isin] = max(Decimal("1"), price * (1 + drift)).quantize(Decimal("0.0001"))


def _add_months(start: date, months: int) -> date:
    y, m = divmod(start.month - 1 + months, 12)
    return date(start.year + y, m + 1, 1)


def generate(seed: int = 42, months: int = 14, start: date = date(2025, 1, 1)) -> SyntheticExport:
    g = _Generator(seed)
    rng = g.rng
    balance = Decimal(0)
    for i in range(months):
        first = _add_months(start, i)
        last = _add_months(start, i + 1) - timedelta(days=1)
        deposit = Decimal(rng.choice((500, 750, 1000)))
        g.deposit(first, deposit)
        balance += deposit
        g.buy(first + timedelta(days=1), WORLD_ETF, spend=Decimal(150))
        if i == 0:
            g.buy(first + timedelta(days=3), DIVIDEND_ETF, shares=Decimal(20))
        if rng.random() < 0.45:
            g.buy(
                first + timedelta(days=rng.randrange(4, 20)),
                rng.choice((US_STOCK, DE_STOCK)),
                shares=Decimal(rng.randrange(1, 6)),
            )
        held = [s for s in (US_STOCK, DE_STOCK) if g.holdings[s.isin] >= 2]
        if held and rng.random() < 0.2:
            instrument = rng.choice(held)
            g.sell(first + timedelta(days=rng.randrange(10, 25)), instrument, Decimal(1))
        if first.month in (3, 6, 9, 12):
            g.dividend(first + timedelta(days=14), US_STOCK)
            g.dividend(first + timedelta(days=20), DIVIDEND_ETF)
        if first.month == 5:
            g.dividend(first + timedelta(days=10), DE_STOCK)
        if first.month == 1 and i > 0:
            g.vorabpauschale(first + timedelta(days=1), WORLD_ETF)
        if first.month == 6:
            g.deposit(first + timedelta(days=5), Decimal(2000), transfer=True)
            balance += 2000
        if first.month == 10:
            g.withdraw(first + timedelta(days=8), Decimal(300))
            balance -= 300
        if first.month == 12:
            g.tax_optimisation(last - timedelta(days=2))
        if i == 2:
            g.stock_perk(first + timedelta(days=12), DE_STOCK, Decimal("25.00"))
        g.interest(last, balance)
        g.move_prices()

    g.rows.sort(key=lambda pair: pair[0])
    rows = [row for _, row in g.rows]
    return SyntheticExport(
        rows=rows,
        holdings={isin: q for isin, q in sorted(g.holdings.items()) if q != 0},
        type_counts=Counter(r["type"] for r in rows),
        cash=g.cash,
    )
