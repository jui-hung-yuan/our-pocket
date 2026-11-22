from sqlalchemy import Connection, Table, insert, select

from our_pocket.db.schema import account, household, person
from our_pocket.etl.models import Account
from our_pocket.etl.registry import default_registry


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
        household_name: str = "household",
    ) -> Account:
        """Create an account; `owner=None` makes it a joint household account."""
        if institution not in self.supported_institutions():
            raise ValueError(
                f"unsupported institution {institution!r}; "
                f"known: {', '.join(self.supported_institutions())}"
            )
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
            )
            .returning(account.c.id)
        ).scalar_one()
        return Account(account_id, name, institution, household_id)

    def get(self, name: str) -> Account:
        row = self.conn.execute(
            select(
                account.c.id, account.c.name, account.c.institution, account.c.household_id
            ).where(account.c.name == name)
        ).one_or_none()
        if row is None:
            raise AccountNotFound(f"no account named {name!r}")
        return Account(*row)

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
