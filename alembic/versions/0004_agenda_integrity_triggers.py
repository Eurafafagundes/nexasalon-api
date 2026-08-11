"""agenda integrity triggers (overlap + bounds cache)

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-11
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------
# Ajuste aprovado 2: checagem de conflito de agenda transacional e segura
# contra concorrência.
#
# Não existe EXCLUDE constraint de sobreposição no banco (a permissão
# `agenda.force_overlap` exige que overlap seja permitido em certos
# casos, e uma constraint é cega a isso). Em vez disso, este trigger:
#
#   1. Serializa concorrência por profissional via
#      `pg_advisory_xact_lock` — qualquer outra transação que tente
#      inserir/atualizar um item do MESMO profissional espera aqui até
#      esta transação terminar (commit ou rollback). Isso fecha a
#      race condition clássica de "checar-depois-inserir" que dois
#      processos concorrentes teriam se a checagem fosse só na aplicação.
#   2. Com a exclusividade garantida, verifica sobreposição contra
#      outros itens do mesmo profissional (ignorando itens/atendimentos
#      cancelados ou no-show).
#   3. Só bloqueia se `app.allow_overlap` não estiver setado como 'true'
#      na sessão — a aplicação seta essa flag quando o usuário autenticado
#      tem a permissão `agenda.force_overlap`.
# ---------------------------------------------------------------------
CHECK_OVERLAP_FUNCTION = """
CREATE OR REPLACE FUNCTION check_appointment_item_overlap() RETURNS trigger AS $$
DECLARE
  conflict_count integer;
  allow_overlap boolean;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.professional_id::text, 0));

  allow_overlap := COALESCE(current_setting('app.allow_overlap', true), 'false') = 'true';

  IF NOT allow_overlap THEN
    SELECT count(*) INTO conflict_count
    FROM appointment_items ai
    JOIN appointments a ON a.id = ai.appointment_id
    WHERE ai.professional_id = NEW.professional_id
      AND ai.id <> NEW.id
      AND COALESCE(ai.status, a.status) NOT IN ('cancelled', 'no_show')
      AND tstzrange(ai.start_at, ai.end_at) && tstzrange(NEW.start_at, NEW.end_at);

    IF conflict_count > 0 THEN
      RAISE EXCEPTION
        'appointment_item_overlap: profissional % já tem um atendimento nesse horário',
        NEW.professional_id
        USING ERRCODE = 'exclusion_violation';
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

CHECK_OVERLAP_TRIGGER = """
CREATE TRIGGER trg_check_appointment_item_overlap
BEFORE INSERT OR UPDATE OF professional_id, start_at, end_at ON appointment_items
FOR EACH ROW EXECUTE FUNCTION check_appointment_item_overlap();
"""

# ---------------------------------------------------------------------
# Ajuste aprovado 3: Appointment.starts_at/ends_at são cache derivado,
# nunca aceitos como entrada do cliente — sempre recalculados a partir
# dos itens, na mesma transação, via trigger (não na camada de
# aplicação, que poderia esquecer de chamar isso em algum fluxo).
# ---------------------------------------------------------------------
RECALC_BOUNDS_FUNCTION = """
CREATE OR REPLACE FUNCTION recalc_appointment_bounds() RETURNS trigger AS $$
DECLARE
  target_appointment_id uuid;
  new_starts_at timestamptz;
  new_ends_at timestamptz;
BEGIN
  target_appointment_id := COALESCE(NEW.appointment_id, OLD.appointment_id);

  SELECT min(start_at), max(end_at)
    INTO new_starts_at, new_ends_at
    FROM appointment_items
    WHERE appointment_id = target_appointment_id;

  UPDATE appointments
     SET starts_at = new_starts_at,
         ends_at = new_ends_at,
         updated_at = now()
   WHERE id = target_appointment_id;

  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

RECALC_BOUNDS_TRIGGER_IUD = """
CREATE TRIGGER trg_recalc_appointment_bounds_iu
AFTER INSERT OR UPDATE OF start_at, end_at, appointment_id ON appointment_items
FOR EACH ROW EXECUTE FUNCTION recalc_appointment_bounds();
"""

RECALC_BOUNDS_TRIGGER_DELETE = """
CREATE TRIGGER trg_recalc_appointment_bounds_d
AFTER DELETE ON appointment_items
FOR EACH ROW EXECUTE FUNCTION recalc_appointment_bounds();
"""


def upgrade() -> None:
    op.execute(CHECK_OVERLAP_FUNCTION)
    op.execute(CHECK_OVERLAP_TRIGGER)
    op.execute(RECALC_BOUNDS_FUNCTION)
    op.execute(RECALC_BOUNDS_TRIGGER_IUD)
    op.execute(RECALC_BOUNDS_TRIGGER_DELETE)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_recalc_appointment_bounds_d ON appointment_items")
    op.execute("DROP TRIGGER IF EXISTS trg_recalc_appointment_bounds_iu ON appointment_items")
    op.execute("DROP FUNCTION IF EXISTS recalc_appointment_bounds()")
    op.execute("DROP TRIGGER IF EXISTS trg_check_appointment_item_overlap ON appointment_items")
    op.execute("DROP FUNCTION IF EXISTS check_appointment_item_overlap()")
