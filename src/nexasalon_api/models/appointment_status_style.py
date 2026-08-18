import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import AppointmentStatus, pg_enum


class AppointmentStatusStyle(Base, UUIDPKMixin, TimestampMixin):
    """Personalização de APRESENTAÇÃO (nome exibido + cor) dos 8 status
    oficiais de `Appointment`, por organização (Configurações > Status
    da Agenda) — nunca um status novo, nunca uma regra de negócio.

    `status_code` é o MESMO enum `AppointmentStatus` usado por
    `Appointment.status`/`services/appointment_state_machine.py` —
    reaproveita o tipo Postgres já existente (`appointment_status`,
    `create_type=False`), não cria um conceito paralelo. Este model não
    tem nenhuma FK partindo de `Appointment`/`AppointmentItem`/`Order`/
    `Payment`/`AuditLog` em direção a ele, e nenhum código de
    `services/appointment_state_machine.py`, `services/orders.py`,
    `services/availability.py` ou `repositories/appointment_item_repo.py`
    o consulta — é uma tabela de LOOKUP de apresentação, lida só pela
    camada de serialização/API pra devolver pro frontend. Trocar o
    `label`/cor de "paid" pra "Recebido" não muda em NADA o
    comportamento de `mark_paid`/`next_status`/Comanda, que continuam
    trabalhando exclusivamente com o código `"paid"` do enum.

    Linha SPARSE de propósito: uma organização que nunca personalizou
    não tem nenhuma linha aqui (não pré-semeamos as 8 combinações) — a
    API devolve só o que existe, e quem chama (frontend) cai pro
    padrão de fábrica (`config/appointment-status.ts`) pra qualquer
    status sem override. `label`/`color_hex` são cada um
    independentemente nullable dentro de uma linha existente: dá pra
    personalizar só a cor sem mexer no nome (ou vice-versa) — um campo
    nulo cai pro default de fábrica mesmo com a linha existindo pro
    outro campo.
    """

    __tablename__ = "appointment_status_styles"
    __table_args__ = (
        UniqueConstraint("organization_id", "status_code", name="uq_appointment_status_styles_org_status"),
        CheckConstraint(
            "color_hex IS NULL OR color_hex ~ '^#[0-9A-Fa-f]{6}$'",
            name="ck_appointment_status_styles_color_hex_format",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    status_code: Mapped[AppointmentStatus] = mapped_column(
        pg_enum(AppointmentStatus, "appointment_status"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(40))
    # Mesmo padrão de `Professional.agenda_color` (schemas/professional.py):
    # String(7) + regex "#RRGGBB" — aqui reforçado também por CHECK no
    # banco (acima), porque esta tabela é escrita só por um endpoint de
    # configuração administrativa (baixo volume, vale a camada extra).
    color_hex: Mapped[str | None] = mapped_column(String(7))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
