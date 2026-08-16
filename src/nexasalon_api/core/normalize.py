"""Normalização de dados de Cliente — telefone e CPF (item "pense desde
agora na qualidade dos dados"). Escopo deliberadamente pequeno: só o
suficiente pra `(61) 99999-9999`, `61999999999` e `+5561999999999`
convergirem pro mesmo valor armazenado/buscável, e pra validar CPF
quando informado. Sem deduplicação automática de cliente nesta rodada
— normalizar já deixa isso possível depois sem mudar schema."""
import re

_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str) -> str:
    """Só dígitos, sem DDI 55 quando presente (`+5561999999999` e
    `61999999999` viram o mesmo `61999999999`) — cobre o caso comum de
    número brasileiro; não tenta ser um normalizador E.164 genérico."""
    digits = _DIGITS.sub("", raw)
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    return digits


def normalize_cpf(raw: str) -> str:
    return _DIGITS.sub("", raw)


def is_valid_cpf(cpf_digits: str) -> bool:
    """Checksum padrão (módulo 11) — `cpf_digits` já deve estar
    normalizado (só dígitos, 11 caracteres)."""
    if len(cpf_digits) != 11 or not cpf_digits.isdigit():
        return False
    if cpf_digits == cpf_digits[0] * 11:  # "00000000000", "11111111111" etc.
        return False

    def _check_digit(base: str) -> int:
        weights = range(len(base) + 1, 1, -1)
        total = sum(int(d) * w for d, w in zip(base, weights))
        remainder = (total * 10) % 11
        return 0 if remainder == 10 else remainder

    d1 = _check_digit(cpf_digits[:9])
    d2 = _check_digit(cpf_digits[:9] + str(d1))
    return cpf_digits[9] == str(d1) and cpf_digits[10] == str(d2)
