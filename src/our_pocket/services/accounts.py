from sqlalchemy import Connection, Table, insert, select

from our_pocket.db.schema import DEFAULT_HOUSEHOLD, account, household, person
from our_pocket.etl.load import BatchStore
from our_pocket.etl.models import Account
from our_pocket.etl.registry import default_registry

_ACCOUNT_COLUMNS = select(
    account.c.id,
    account.c.name,
    account.c.institution,
    account.c.household_id,
    account.c.export_account_name,
)


class AccountNotFound(LookupError):
    pass


class AccountService:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    @staticmethod
    def supported_institutions() -> tuple[str, ...]:
        return tuple(sorted({t.institution for t in default_registry()}))

    def add(
        self,
        name: str,
        institution: str,
        *,
        owner: str | None = None,
        household_name: str = DEFAULT_HOUSEHOLD,
        export_account_name: str | None = None,
    ) -> Account:
        """Create an account; `owner=None` makes it a joint household account."""
        if institution not in self.supported_institutions():
            raise ValueError(
                f"unsupported institution {institution!r}; "
                f"known: {', '.join(self.supported_institutions())}"
            )
        export_account_name = self._check_export_name(institution, export_account_name)
        household_id = self._get_or_create(household, name=household_name)
        owner_id = (
            self._get_or_create(person, household_id=household_id, name=owner) if owner else None
        )
        account_id = self.conn.execute(
            insert(account)
            .values(
                household_id=household_id,
                owner_person_id=owner_id,
                institution=institution,
                name=name,
                export_account_name=export_account_name,
            )
            .returning(account.c.id)
        ).scalar_one()
        return Account(account_id, name, institution, household_id, export_account_name)

    def get(self, name: str) -> Account:
        row = self.conn.execute(_ACCOUNT_COLUMNS.where(account.c.name == name)).one_or_none()
        if row is None:
            raise AccountNotFound(f"no account named {name!r}")
        return Account(*row)

    def by_export_name(
        self, institution: str, household_name: str = DEFAULT_HOUSEHOLD
    ) -> dict[str, Account]:
        """One household's accounts of `institution`, keyed by their name inside the export."""
        return BatchStore(self.conn).accounts_by_export_name(institution, household_name)

    @staticmethod
    def _check_export_name(institution: str, export_account_name: str | None) -> str | None:
        """Routed formats (N26) need the account's name in the export; others can't use one."""
        routed = {t.institution for t in default_registry() if t.account_column is not None}
        name = (export_account_name or "").strip()
        if institution in routed and not name:
            raise ValueError(
                f"{institution} accounts need --export-name, the space name as in the export"
            )
        if institution not in routed and export_account_name is not None:
            raise ValueError(f"{institution} exports name no account; drop --export-name")
        return name or None

    def _get_or_create(self, table: Table, **values: object) -> int:
        existing: int | None = self.conn.execute(
            select(table.c.id).where(*(table.c[k] == v for k, v in values.items()))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        created: int = self.conn.execute(
            insert(table).values(**values).returning(table.c.id)
        ).scalar_one()
        return created
