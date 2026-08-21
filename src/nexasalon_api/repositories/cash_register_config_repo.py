import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.cash_register_config import CashRegisterConfig


def get(session: Session, organization_id: uuid.UUID) -> CashRegisterConfig | None:
    stmt = select(CashRegisterConfig).where(CashRegisterConfig.organization_id == organization_id)
    return session.scalars(stmt).first()


def upsert(
    session: Session, organization_id: uuid.UUID, *, values: dict[str, Any], updated_by: uuid.UUID | None
) -> CashRegisterConfig:
    config = get(session, organization_id)
    if config is None:
        config = CashRegisterConfig(organization_id=organization_id)
        session.add(config)
    for key, value in values.items():
        setattr(config, key, value)
    config.updated_by = updated_by
    session.flush()
    return config
