from fastapi import APIRouter, Depends, Request, Response, status

from nexasalon_api.api.deps import rate_limit_signup
from nexasalon_api.api.v1.auth import _set_refresh_cookie, _tokens_to_schema
from nexasalon_api.schemas.signup import SignupRequest, SignupResponse
from nexasalon_api.services import signup as signup_service

router = APIRouter(prefix="/signup", tags=["signup"])


@router.post(
    "",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Criar organização trial e usuário owner",
    dependencies=[Depends(rate_limit_signup)],
)
def signup(
    payload: SignupRequest, request: Request, response: Response
) -> SignupResponse:
    del request  # usado pela dependency de rate limit
    result = signup_service.create_account(payload)
    _set_refresh_cookie(response, result.tokens.refresh_token)
    return SignupResponse(tokens=_tokens_to_schema(result.tokens))
