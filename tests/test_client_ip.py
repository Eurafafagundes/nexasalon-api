"""Etapa 3C — `api/deps.py::_client_ip` não pode confiar em nenhuma
posição de `X-Forwarded-For` além da primeira, e essa confiança em si
é uma estratégia configurável (`settings.client_ip_strategy`), não uma
verdade fixa no código — ver docstring da função e README > "IP real
do cliente e rate limiting" para a topologia específica do Render que
motiva isso, e para o modo conservador `socket_only`."""
from starlette.requests import Request

from nexasalon_api.api.deps import _client_ip
from nexasalon_api.core.config import settings


def _make_request(headers: dict[str, str], client_host: str | None = "10.0.0.5") -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


def test_usa_o_primeiro_elemento_do_x_forwarded_for():
    request = _make_request({"x-forwarded-for": "203.0.113.9, 1.1.1.1, 2.2.2.2"})
    assert _client_ip(request) == "203.0.113.9"


def test_ignora_entradas_forjadas_depois_da_primeira():
    # Mesmo IP real (posição 0), cauda forjada DIFERENTE — como o Render
    # nunca limpa o que o cliente mandou, um atacante pode variar a
    # cauda à vontade; a chave de rate limit não pode mudar por causa
    # disso, senão o limite vira inútil (basta variar a cauda a cada
    # tentativa pra "resetar" o contador).
    a = _client_ip(_make_request({"x-forwarded-for": "203.0.113.9, forjado-a"}))
    b = _client_ip(_make_request({"x-forwarded-for": "203.0.113.9, forjado-b, forjado-c"}))
    assert a == b == "203.0.113.9"


def test_ips_reais_diferentes_geram_chaves_diferentes():
    a = _client_ip(_make_request({"x-forwarded-for": "203.0.113.9, x"}))
    b = _client_ip(_make_request({"x-forwarded-for": "203.0.113.10, x"}))
    assert a != b


def test_sem_header_cai_no_peer_tcp_direto():
    # Dev local/testes: não há proxy na frente, o header nem existe.
    request = _make_request({}, client_host="127.0.0.1")
    assert _client_ip(request) == "127.0.0.1"


def test_sem_header_e_sem_client_devolve_unknown():
    request = _make_request({}, client_host=None)
    assert _client_ip(request) == "unknown"


def test_default_strategy_e_trust_first_proxy_hop():
    # Trocar o default sem querer é o tipo de regressão que este teste
    # existe pra pegar: staging depende de rate limit por IP real
    # funcionando, não travado no modo conservador.
    assert settings.client_ip_strategy == "trust_first_proxy_hop"


def test_socket_only_ignora_x_forwarded_for_por_completo(monkeypatch):
    # Modo de fallback conservador (Etapa 3C, revisão pedida pelo
    # usuário): se a suposição sobre a borda da Render for contestada,
    # trocar pra este modo por config deve bastar — sem X-Forwarded-For
    # nenhum, só o peer TCP.
    monkeypatch.setattr(settings, "client_ip_strategy", "socket_only")
    request = _make_request({"x-forwarded-for": "203.0.113.9, forjado"}, client_host="10.0.0.5")
    assert _client_ip(request) == "10.0.0.5"


def test_socket_only_sem_client_devolve_unknown(monkeypatch):
    monkeypatch.setattr(settings, "client_ip_strategy", "socket_only")
    request = _make_request({"x-forwarded-for": "203.0.113.9"}, client_host=None)
    assert _client_ip(request) == "unknown"
