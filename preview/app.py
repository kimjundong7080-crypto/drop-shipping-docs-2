from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config import cfg
from db import get_conn
from services import order_service
from utils.security import require_api_key, check_api_key

# P1-14: 앱 전역 require_api_key 제거 — API 라우트에만 키를 요구한다.
# 브라우저는 X-API-Key 헤더를 보낼 수 없으므로 HTML 미리보기는 별도 게이트를 쓴다.
app = FastAPI(
    title="Dropship Preview",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,  # 보완사항 §3: 디버그 엔드포인트 비활성화
)
templates = Jinja2Templates(directory="preview/templates")


def _html_access_ok(request: Request) -> None:
    """HTML 라우트 접근 제어 (보완사항 §3 루프백 정책).

    - 기본: 서버 바인딩(cfg.local_bind_host)이 루프백이면 localhost 전용 미리보기
      이므로 네트워크 노출 자체가 없다 → HTML 라우트를 키 없이 허용(브라우저 사용).
    - 0.0.0.0/:: 등 비루프백으로 바인드하면(터널 포함 — uvicorn 은 request.client 를
      항상 프록시 주소 127.0.0.1 로 보고하므로 클라이언트 IP 검사는 무의미하다)
      ?key=... 또는 X-API-Key 검사에 통과해야 한다(fail-closed).
    """
    bind_is_loopback = cfg.local_bind_host in ("127.0.0.1", "0", "::1", "localhost")
    key = request.query_params.get("key") or request.headers.get("x-api-key") or ""
    if bind_is_loopback and not key:
        return
    if key and check_api_key(key):
        return
    raise HTTPException(status_code=401,
                        detail="non-loopback binding requires ?key= or X-API-Key")


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def preview_order(request: Request, order_id: int):
    _html_access_ok(request)
    try:
        data = order_service.preview_order(order_id)
    except ValueError:
        raise HTTPException(404)
    # Starlette 1.x 호환: TemplateResponse(request, name, context) 시그니처 사용
    return templates.TemplateResponse(
        request, "order.html", data
    )


@app.get("/api/orders/{order_id}", dependencies=[Depends(require_api_key)])
def api_order(order_id: int):
    try:
        return order_service.preview_order(order_id)
    except ValueError:
        raise HTTPException(404)


@app.get("/api/products/{canonical_sku}", dependencies=[Depends(require_api_key)])
def api_product(canonical_sku: str):
    with get_conn() as conn:
        prod = conn.execute(
            "SELECT * FROM canonical_products WHERE canonical_sku = ?",
            (canonical_sku,),
        ).fetchone()
        variants = conn.execute(
            "SELECT * FROM channel_variants WHERE canonical_sku = ?",
            (canonical_sku,),
        ).fetchall()
    if not prod:
        raise HTTPException(404)
    return {"product": dict(prod), "variants": [dict(v) for v in variants]}


@app.get("/health")
def health():
    return {"ok": True}
