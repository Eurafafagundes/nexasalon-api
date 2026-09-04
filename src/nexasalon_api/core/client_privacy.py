"""Mascaramento centralizado de dados de CONTATO de Client (telefone,
WhatsApp, CPF, e-mail, endereço) — item real de negócio: já houve
funcionário com acesso à própria Agenda que usava o telefone de
clientes pra oferecer serviço por fora. `clients.view_contact_data` é a
ÚNICA permission que libera os valores reais; toda rota que pode
devolver um `Client` (lookup, detalhe, listagem, Ficha 360°, criação,
edição, comprovante de comanda) passa por este MESMO helper — nunca
duplica a regra de mascaramento em cada endpoint. `name` NUNCA é
mascarado (item explícito do pedido: "pode ver o nome da cliente" sem
essa permission) — só os campos de contato/identificação sensível.

Mascarar aqui, no valor devolvido pela API, é a proteção REAL — nunca
confiar em esconder só no frontend (alguém no DevTools/network não pode
recuperar o dado completo, porque ele nunca sai do backend)."""
from typing import TypeVar

from pydantic import BaseModel

CONTACT_PERMISSION = "clients.view_contact_data"

_HIDDEN = "Oculto"
_CPF_MASK = "***.***.***-**"

_ADDRESS_FIELDS = (
    "cep",
    "state",
    "city",
    "neighborhood",
    "address_line",
    "address_number",
    "complement",
)


def client_can_view_contact(permissions: frozenset[str] | set[str]) -> bool:
    return CONTACT_PERMISSION in permissions


def _mask_phone(value: str | None) -> str | None:
    """Mantém só os 4 últimos dígitos (`core/normalize.py` já garante
    que o valor armazenado é só dígitos) — suficiente pra reconhecer
    "é essa cliente mesmo" sem devolver o número completo."""
    if not value:
        return value
    if len(value) <= 4:
        return _HIDDEN
    return f"(**) *****-{value[-4:]}"


def _mask_cpf(value: str | None) -> str | None:
    if not value:
        return value
    return _CPF_MASK


def _mask_present(value: str | None) -> str | None:
    """E-mail: existir ou não já é o suficiente pro frontend decidir
    "Oculto" vs "Não informado" — sem revelar nenhum caractere real."""
    if not value:
        return value
    return _HIDDEN


_ClientReadT = TypeVar("_ClientReadT", bound=BaseModel)


def apply_client_contact_masking(client_read: _ClientReadT, *, can_view_contact: bool) -> _ClientReadT:
    """Recebe um `ClientRead`/`ClientListRead` já validado e devolve a
    versão pronta pra resposta HTTP — mascarada quando o ator não tem
    `clients.view_contact_data`, intacta (mesma instância) quando tem.
    Endereço é zerado por completo (nunca um mix de campos reais/
    mascarados) — o frontend usa `can_view_contact_data` (devolvido
    junto) pra decidir entre "Oculto" e "Não informado" na UI."""
    if can_view_contact:
        return client_read.model_copy(update={"can_view_contact_data": True})
    updates: dict[str, object] = {
        "phone": _mask_phone(getattr(client_read, "phone", None)),
        "cpf": _mask_cpf(getattr(client_read, "cpf", None)),
        "can_view_contact_data": False,
    }
    if hasattr(client_read, "whatsapp"):
        updates["whatsapp"] = _mask_phone(client_read.whatsapp)
    if hasattr(client_read, "email"):
        updates["email"] = _mask_present(client_read.email)
    for field in _ADDRESS_FIELDS:
        if hasattr(client_read, field):
            updates[field] = None
    return client_read.model_copy(update=updates)


def mask_receipt_contact(*, phone: str | None, email: str | None, can_view_contact: bool) -> tuple[str | None, str | None]:
    """Mesma regra pro Comprovante de Atendimento (`OrderReceiptRead`) —
    schema próprio (`ReceiptClient`), sem herdar de `ClientRead`, então
    não passa por `apply_client_contact_masking` — reaproveita só os
    helpers de valor, pra nunca duplicar o formato da máscara."""
    if can_view_contact:
        return phone, email
    return _mask_phone(phone), _mask_present(email)
