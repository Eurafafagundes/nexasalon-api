import uuid
from datetime import time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import BrazilianState, OrganizationStatus, pg_enum


class Organization(Base, UUIDPKMixin, TimestampMixin):
    """Tenant raiz. Cada salão/empresa é uma Organization.

    Sem `owner_id`: propriedade é expressa via
    `OrganizationMembership.role = owner` — um segundo ponteiro de "dono"
    seria mais uma fonte de verdade duplicada (mesmo problema que o
    ajuste 1 resolveu para Professional/Membership).

    Etapa D ("Informações do Estabelecimento") — antes de adicionar
    colunas novas, o modelo existente foi inspecionado (item explícito
    "não duplique o que já existir", mesmo raciocínio já aplicado ao
    Client na Etapa C.1). Mapeamento decidido:

      REUSADOS (já existiam, sem uso concreto em código antes desta
      etapa — `document`/`business_type` nunca eram lidos/escritos em
      lugar nenhum, confirmado por grep):
        - `name`       -> nome fantasia (já é o nome de exibição da org)
        - `document`   -> CNPJ (genérico o bastante, cabe perfeitamente)
        - `business_type` -> categoria do estabelecimento
        - `phone`, `email`, `timezone` -> mesmos campos, reusados direto

      NOVOS (conceito que não existia antes):
        - `legal_name` (razão social — distinto de `name`/nome fantasia)
        - `logo_url` (upload via `core/storage.py`)
        - `cep`/`state`/`city`/`neighborhood`/`address_line`/
          `address_number`/`complement` (Organization não tinha NENHUM
          campo de endereço — `Branch` tem os seus, propositalmente
          separados, ver docstring de `Branch` abaixo)
        - `whatsapp` (só existia `phone`/`email` genéricos)
        - `instagram`, `website`

    Todos os campos novos são NULLABLE/opcionais — "campos devem ser
    opcionais quando não forem tecnicamente indispensáveis" e "não
    impedir operação do salão porque CNPJ/endereço não foi preenchido"
    são requisitos explícitos do pedido.
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # Reusado como CNPJ nesta etapa — ver docstring da classe.
    document: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="America/Sao_Paulo")
    # Texto livre de propósito, NUNCA um enum fechado — serve só de
    # metadado pra onboarding/templates/linguagem de interface (ex.:
    # "barbearia", "salao_beleza", "estetica", "nail_designer", "spa").
    # Nenhuma regra de negócio, serviço ou funcionalidade pode ficar
    # condicionada a este valor: uma barbearia pode cadastrar "Massagem",
    # um salão pode cadastrar "Barba" — o sistema nunca impede. Reusado
    # nesta etapa como "categoria do estabelecimento".
    business_type: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[OrganizationStatus] = mapped_column(
        pg_enum(OrganizationStatus, "organization_status"),
        nullable=False,
        server_default=OrganizationStatus.TRIAL.value,
    )

    # --- Etapa D: campos novos ---
    legal_name: Mapped[str | None] = mapped_column(String(255))
    logo_url: Mapped[str | None] = mapped_column(String(500))
    cep: Mapped[str | None] = mapped_column(String(8))
    # Reusa o MESMO tipo enum `brazilian_state` já criado pra
    # `Client.state` na migration 0015 — nunca cria um segundo enum
    # equivalente.
    state: Mapped[BrazilianState | None] = mapped_column(pg_enum(BrazilianState, "brazilian_state"))
    city: Mapped[str | None] = mapped_column(String(120))
    neighborhood: Mapped[str | None] = mapped_column(String(120))
    address_line: Mapped[str | None] = mapped_column(String(255))
    address_number: Mapped[str | None] = mapped_column(String(20))
    complement: Mapped[str | None] = mapped_column(String(120))
    whatsapp: Mapped[str | None] = mapped_column(String(32))
    instagram: Mapped[str | None] = mapped_column(String(120))
    website: Mapped[str | None] = mapped_column(String(255))

    # --- Etapa K: Agendamento Online público (Configurações > Agendamento
    # Online) — página nasce DESATIVADA (`false`) até a organização optar
    # por ligá-la explicitamente; `online_booking_auto_confirm` nasce
    # LIGADA (comportamento mais comum de agendamento online: confirma
    # sozinho, sem exigir um passo manual de "confirmar" da recepção —
    # quando desligado, o agendamento público nasce em SCHEDULED normal,
    # igual a um agendamento interno comum). `slug` (usado na URL pública,
    # `/agendar/<slug>`) JÁ EXISTIA desde a migration 0002 — não duplicado
    # aqui, só passa a ser editável via `OrganizationUpdate` nesta etapa.
    online_booking_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    online_booking_auto_confirm: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # Antecedência mínima/máxima para reservar um horário pela página
    # pública — puramente uma regra de UX/negócio desta etapa (a
    # disponibilidade REAL continua vindo integralmente do motor
    # existente, `services/availability.py`; estes dois campos só
    # recortam a janela de datas ofertada/aceita no fluxo público).
    online_booking_min_lead_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    online_booking_max_lead_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    # Etapa M — "Permitir agendamento para o mesmo dia" (Configurações >
    # Agendamento Online > Regras de agendamento). `true` (default,
    # comportamento IDÊNTICO ao de antes desta coluna) = a antecedência
    # mínima acima já governa hoje normalmente. `false` = hoje nunca é
    # oferecido, mesmo que `online_booking_min_lead_minutes` fosse baixo
    # o bastante pra permitir — o primeiro horário possível vira amanhã
    # 00:00 (ainda recortado pelo horário de funcionamento/jornada
    # normalmente). Ver `services/public_booking.py::_lead_time_bounds`
    # e `services/appointments.py::_assert_online_booking_lead_time`.
    online_booking_same_day_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    branches: Mapped[list["Branch"]] = relationship(back_populates="organization")


class BusinessHours(Base, UUIDPKMixin, TimestampMixin):
    """Etapa M — Horário de funcionamento do ESTABELECIMENTO
    (Configurações > Informações do Estabelecimento), camada SUPERIOR à
    jornada do profissional (`WorkingHours`): a disponibilidade real de
    qualquer profissional é sempre `funcionamento ∩ jornada ∩
    bloqueios ∩ conflitos`, nunca só a jornada isolada — ver
    `services/availability.py::effective_working_windows_utc`, o único
    ponto que combina as duas coisas (Agenda interna, Novo Agendamento e
    Agendamento Online reaproveitam essa mesma função, nunca uma segunda
    lógica por tela).

    Por ORGANIZATION (não por `Branch`) — a tela de configurações é uma
    só por estabelecimento, mesma decisão já tomada pela página pública
    (`services/public_booking.py::get_default_branch`, primeira unidade
    ativa). Uma linha por dia da semana (0=domingo…6=sábado, mesma
    convenção de `WorkingHours`) — sem turno partido aqui (é só
    aberto/fechado + um intervalo, não a jornada real de ninguém).

    Ausência de QUALQUER linha para a organização = sem restrição
    nenhuma (comportamento idêntico ao de antes desta tabela existir —
    "dados existentes precisam continuar funcionando"); a restrição só
    passa a valer depois que o proprietário salva a tela pela primeira
    vez (grava as 7 linhas de uma vez, ver
    `repositories/business_hours_repo.py::replace_all`)."""

    __tablename__ = "business_hours"
    __table_args__ = (
        UniqueConstraint("organization_id", "weekday"),
        CheckConstraint(
            "(is_open = false AND start_time IS NULL AND end_time IS NULL) OR "
            "(is_open = true AND start_time IS NOT NULL AND end_time IS NOT NULL AND start_time < end_time)",
            name="business_hours_open_consistency",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0=domingo … 6=sábado
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)


class Branch(Base, UUIDPKMixin, TimestampMixin):
    """Unidade física de uma Organization.

    `agenda_view_start`/`agenda_view_end`/`agenda_slot_minutes` são
    configuração de APRESENTAÇÃO da grade da Agenda principal (que
    janela de horas desenhar, em que granularidade) — cada unidade
    define a sua, nada fixo no código. Isto é puramente visual: NÃO
    substitui `WorkingHours` (que continua sendo a única fonte de
    disponibilidade real de cada profissional) nem afeta duração de
    serviço/buffer. Os defaults (`07:00`–`21:00`, 30 min) existem só
    como valor inicial de compatibilidade para unidades já cadastradas
    antes desta migration — não são uma regra de negócio."""

    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug"),
        CheckConstraint("agenda_view_start < agenda_view_end", name="agenda_view_start_before_end"),
        CheckConstraint("agenda_slot_minutes IN (15, 30)", name="agenda_slot_minutes_allowed_values"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    address_line: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(2))
    zip_code: Mapped[str | None] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str | None] = mapped_column(String(64))  # herda o da Organization quando nulo
    agenda_view_start: Mapped[time] = mapped_column(Time, nullable=False, server_default="07:00:00")
    agenda_view_end: Mapped[time] = mapped_column(Time, nullable=False, server_default="21:00:00")
    agenda_slot_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="30")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")

    organization: Mapped["Organization"] = relationship(back_populates="branches")
