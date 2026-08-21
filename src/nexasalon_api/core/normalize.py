"""Normalização de dados de Cliente e de Organization — telefone, CPF e
CNPJ (item "pense desde agora na qualidade dos dados"). Escopo
deliberadamente pequeno: só o suficiente pra `(61) 99999-9999`,
`61999999999` e `+5561999999999` convergirem pro mesmo valor
armazenado/buscável, e pra validar CPF/CNPJ quando informado. Sem
deduplicação automática de cliente nesta rodada — normalizar já deixa
isso possível depois sem mudar schema.

`normalize_cnpj`/`is_valid_cnpj` (Etapa D — "Informações do
Estabelecimento") seguem o MESMO raciocínio já aplicado a CPF na Etapa
C.1: nunca confiar só na máscara do frontend, o backend sempre
normaliza (só dígitos) e valida (checksum módulo 11) antes de
persistir."""
import re
import unicodedata

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


def normalize_slug(raw: str) -> str:
    """Slug de URL (Etapa K — Agendamento Online): minúsculo, só
    `[a-z0-9-]`, hífens colapsados/sem sobra nas pontas. `"Salão da
    Ana!"` -> `"salao-da-ana"` (troca espaço por hífen, remove tudo que
    não é alfanumérico/hífen — inclusive acento, já que `\\w` do Python
    não cobre isso sozinho, então normaliza NFKD antes)."""
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_cnpj(raw: str) -> str:
    return _DIGITS.sub("", raw)


def is_valid_cnpj(cnpj_digits: str) -> bool:
    """Checksum padrão (módulo 11, pesos 5-2 e 6-2) — `cnpj_digits` já
    deve estar normalizado (só dígitos, 14 caracteres)."""
    if len(cnpj_digits) != 14 or not cnpj_digits.isdigit():
        return False
    if cnpj_digits == cnpj_digits[0] * 14:  # "00000000000000" etc.
        return False

    def _check_digit(base: str) -> int:
        weights = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2][-len(base):]
        total = sum(int(d) * w for d, w in zip(base, weights))
        remainder = total % 11
        return 0 if remainder < 2 else 11 - remainder

    d1 = _check_digit(cnpj_digits[:12])
    d2 = _check_digit(cnpj_digits[:12] + str(d1))
    return cnpj_digits[12] == str(d1) and cnpj_digits[13] == str(d2)
