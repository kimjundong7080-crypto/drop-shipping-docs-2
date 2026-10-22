import hmac
import hashlib
import base64
from fastapi import Header, HTTPException
from config import cfg


def verify_cj(raw_body: bytes, sign_header: str) -> bool:
    if not sign_header:
        return False
    expected = base64.b64encode(
        hmac.new(cfg.cj_open_id.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(sign_header, expected)


def verify_shopify(raw_body: bytes, hmac_header: str) -> bool:
    if not hmac_header:
        return False
    computed = hmac.new(
        cfg.shopify_client_secret.encode(), raw_body, hashlib.sha256
    ).digest()
    try:
        # validate=True: padding/문자 변조로 디코딩이 조용히 통과하는 것을 방지
        provided = base64.b64decode(hmac_header, validate=True)
    except Exception:
        return False
    return hmac.compare_digest(computed, provided)


def verify_woo(raw_body: bytes, sign_header: str, secret: str) -> bool:
    if not sign_header:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(sign_header, expected)


def _api_key_expected_bytes() -> bytes:
    """P2-20: LOCAL_API_KEY 미설정 시 기동/요청 거부(fail-closed)."""
    if not cfg.local_api_key:
        raise HTTPException(status_code=503,
                            detail="LOCAL_API_KEY not configured (fail-closed)")
    return cfg.local_api_key.encode("utf-8")


def check_api_key(provided: str | None) -> bool:
    """P2-20: compare_digest 는 비ASCII str 에서 TypeError — 바이트로 정규화 후 비교."""
    expected = _api_key_expected_bytes()
    if not provided:
        # 상수시간 유지를 위해 제공값도 같은 길이 더미로 비교
        hmac.compare_digest(b"", expected)
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected)


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not check_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="invalid api key")
