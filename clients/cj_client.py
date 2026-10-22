import json
import os
import tempfile
import time
from pathlib import Path

import requests
from tenacity import (
    retry, wait_exponential, stop_after_attempt,
    retry_if_exception_type,
)

from config import cfg, require
from utils.throttle import TokenBucket


def _token_cache_path() -> Path:
    """P2-17: 작업 디렉터리가 아닌 사용자 캐시 디렉터리에 저장."""
    base = Path(os.environ.get(
        "XDG_CACHE_HOME", Path.home() / ".cache"
    )) / "dropship-sync"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base / "cj_token.json"


class TransientAPIError(Exception):
    pass


class CJAuthError(Exception):
    """401 — 토큰 갱신 후 1회만 재시도."""


class CJAPIError(RuntimeError):
    """RUN-REPORT P2-3: HTTP 4xx/5xx 등 확정 실패.

    requests.HTTPError 는 TransientAPIError(5xx)와 달리 tenacity 재시도 대상이
    아니고 place_order 의 except 절에도 포착되지 않아 UNCERTAIN 으로 오분류된다.
    4xx 거부 + result=false 응답은 '확정 실패'이므로 이 타입으로 구분해
    FAILED 기록·재시도 허용 경로로 보낸다 (보완사항 §6 분류 원칙).
    """


def _check_http_status(resp: "requests.Response") -> None:
    """raise_for_status 대신 사용 — 4xx(401 제외)/5xx 를 CJAPIError 로 변환.

    5xx 는 TransientAPIError(재시도 대상)을 유지하고, raise_for_status 가
    requests.HTTPError 를 던져 호출 측 분류를 우회하는 것을 막는다.
    """
    if resp.status_code >= 400 and resp.status_code != 401:
        body = (resp.text or "")[:300]
        if resp.status_code in (500, 502, 503, 504):
            raise TransientAPIError(f"CJ transient {resp.status_code}: {body}")
        raise CJAPIError(f"CJ HTTP {resp.status_code}: {body}")


class CJClient:
    def __init__(self):
        require("cj")
        self.session = requests.Session()
        self.bucket = TokenBucket(rate=cfg.cj_qps)
        self._token: str | None = self._load_token()
        self._auth_retries_left = 1

    # ---- 토큰 캐시 (원자적 쓰기 + 0600) ----
    def _load_token(self) -> str | None:
        path = _token_cache_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if data.get("expiry", 0) > time.time() + 60:
                    return data["token"]
            except Exception:
                pass
        return None

    def _save_token(self, token: str, expiry: float) -> None:
        path = _token_cache_path()
        fd, tmp = tempfile.mkstemp(dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"token": token, "expiry": expiry}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)  # 원자적 교체 — 동시 프로세스 경합 방지
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _authenticate(self) -> str:
        resp = self.session.post(
            f"{cfg.cj_base}/authentication/getAccessToken",
            json={"email": cfg.cj_email, "password": cfg.cj_api_key},
            timeout=30,
        )
        _check_http_status(resp)  # P2-3: HTTPError 대신 CJAPIError/Transient 분류
        body = resp.json()
        if not body.get("result"):
            raise CJAPIError(f"CJ auth failed: {body}")
        token = body["data"]["accessToken"]
        expiry = time.time() + body["data"].get("expiresIn", 3600) - 120
        self._save_token(token, expiry)
        return token

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self._authenticate()
        return self._token

    def _raw_request(self, method: str, path: str, *, params: dict | None = None,
                     json_body: dict | None = None) -> dict:
        self.bucket.acquire()
        resp = self.session.request(
            method,
            f"{cfg.cj_base}{path}",
            headers={"CJ-Access-Token": self.token},
            params=params or {},
            json=json_body,
            timeout=60,
        )
        if resp.status_code == 401:
            raise CJAuthError("CJ token rejected")
        if resp.status_code == 429:
            raise TransientAPIError("CJ transient 429")
        _check_http_status(resp)  # RUN-REPORT P2-3: HTTPError 우회 방지
        body = resp.json()
        if not body.get("result"):
            # 확정 거부(result=false)는 재시도·UNCERTAIN 이 아닌 실패로 분류
            raise CJAPIError(f"CJ error {path}: {body}")
        return body

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None) -> dict:
        """재시도(tenacity) + 401 시 토큰 갱신 1회 재시도만 허용(무한 재귀 방지)."""
        try:
            return self._request_retry(method, path, params=params, json_body=json_body)
        except CJAuthError:
            if self._auth_retries_left <= 0:
                raise RuntimeError("CJ auth failed after one token refresh")
            self._auth_retries_left -= 1
            _token_cache_path().unlink(missing_ok=True)
            self._token = self._authenticate()
            return self._request_retry(method, path, params=params, json_body=json_body)

    @retry(
        retry=retry_if_exception_type(TransientAPIError),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _request_retry(self, method: str, path: str, *, params: dict | None = None,
                       json_body: dict | None = None) -> dict:
        return self._raw_request(method, path, params=params, json_body=json_body)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.request("GET", path, params=params)["data"]

    # ---- 상품 관련 ----
    def list_products(self, keyword: str, page: int = 1, size: int = 50) -> list[dict]:
        data = self.get(
            "/product/list",
            {"productNameEn": keyword, "pageNum": page, "pageSize": size},
        )
        return data.get("list", [])

    def variant_query(self, pid: str) -> dict:
        return self.get("/product/variant/query", {"pid": pid})

    # ---- 운임 / 물류 ----
    def calculate_freight(self, payload: dict) -> dict:
        return self.request(
            "POST", "/logistic/freightCalculate", json_body=payload
        ).get("data", {})

    # ---- 주문 ----
    def create_order(self, payload: dict, sandbox: bool = False) -> dict:
        """P0-5: 샌드박스/실주문 모두 이 경로로 통합. 응답 본문 전체를 반환."""
        return self.request("POST", "/shopping/order/createOrder", json_body=payload)

    def query_order(self, cj_order_id: str) -> dict:
        return self.get("/shopping/order/getOrderDetail", {"orderId": cj_order_id})
