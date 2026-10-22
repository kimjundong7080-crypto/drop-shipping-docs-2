import argparse
import logging
import sys

import uvicorn

from config import cfg
from db import init_db
from utils.logging_setup import setup_logging
from utils.audit import verify_audit_chain

log = logging.getLogger(__name__)


def cmd_preview(args):
    from preview.app import app
    uvicorn.run(app, host=cfg.local_bind_host, port=cfg.local_bind_port)


def cmd_webhook(args):
    from webhook.receiver import app
    # P2-22: --port 기본값을 설정(cfg.webhook_bind_port)과 통일
    port = args.port if args.port is not None else cfg.webhook_bind_port
    uvicorn.run(app, host=cfg.local_bind_host, port=port)


def cmd_sync_products(args):
    if args.pages < 1:  # P2-22: 최소값 검증
        raise SystemExit("--pages must be >= 1")
    from services.product_sync import sync_by_keyword
    for result in sync_by_keyword(args.keyword, pages=args.pages):
        print(result)


def cmd_sync_inventory(args):
    from services.inventory_sync import sync_inventory
    print(sync_inventory())


def cmd_orders_collect(args):
    from services import order_service
    if args.channel in ("all", "woocommerce"):
        print("woo:", order_service.collect_woo())
    if args.channel in ("all", "shopify"):
        print("shopify:", order_service.collect_shopify())


def cmd_orders_preview(args):
    from services.order_service import preview_order
    import json
    print(json.dumps(preview_order(args.order_id), indent=2, default=str))


def cmd_orders_place(args):
    from services.order_service import place_order, sandbox_preview
    if args.sandbox_first:
        print("sandbox:", sandbox_preview(args.order_id))
    print("place:", place_order(args.order_id, confirm=args.confirm,
                                sandbox=args.confirm_cj_sandbox))


def cmd_orders_tracking(args):
    from services.order_service import push_tracking
    print(push_tracking(args.order_id))


def cmd_disputable(args):
    """분쟁 가능 상품 목록 조회 (CJ 분쟁 ①단계)."""
    import json as _json
    from services.dispute_service import list_disputable
    print(_json.dumps(list_disputable(args.order_id), indent=2, ensure_ascii=False))


def cmd_dispute_create(args):
    """분쟁 생성 (②확인→③생성). --confirm 없으면 dry-run + 한도 검증만 수행."""
    from services.dispute_service import create_dispute
    print(create_dispute(
        args.order_id, args.line_id, args.reason, args.amount,
        refund=(args.kind == "refund"), confirm=args.confirm,
    ))


def cmd_profit_report(args):
    import csv
    import sys as _sys
    from services.cost_ledger import profit_report
    rows = profit_report(limit=args.limit)
    if not rows:
        print("no orders in ledger")
        return
    writer = csv.DictWriter(_sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)


def cmd_listing(args):
    from services.listing_service import set_listing
    channels = [c.strip() for c in args.channels.split(",")]
    print(set_listing(args.sku, channels, visible=(args.action == "publish")))


def cmd_comments_sync(args):
    from services.comment_service import sync_woo_reviews
    print("synced:", sync_woo_reviews())


def cmd_comments_draft(args):
    from services.comment_service import draft_reply
    draft_reply(args.comment_id, args.body)
    print("draft saved")


def cmd_comments_send(args):
    from services.comment_service import send_reply
    print(send_reply(args.comment_id))


def cmd_audit_verify(args):
    print("audit chain valid:", verify_audit_chain())


def cmd_webhook_worker(args):
    from webhook.worker import run_loop
    run_loop(interval_sec=args.interval, once=args.once)


def cmd_doctor(args):
    print("audit:", verify_audit_chain())
    from clients.cj_client import CJClient
    CJClient().token
    print("cj: token ok")
    from clients.shopify_client import ShopifyClient
    ShopifyClient().execute("query { shop { name } }")
    print("shopify: ok")
    from clients import woo_client
    woo_client.wcapi.get("products", params={"per_page": 1})
    print("woocommerce: ok")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dropship")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("preview"); sp.set_defaults(func=cmd_preview)
    sp = sub.add_parser("webhook")
    sp.add_argument("--port", type=int, default=None,
                    help="미지정 시 .env WEBHOOK_BIND_PORT(기본 8001) 사용")
    sp.set_defaults(func=cmd_webhook)

    sp = sub.add_parser("webhook-worker",
                        help="webhook_events 큐 소비 워커 (P2-19)")
    sp.add_argument("--interval", type=int, default=5, help="폴링 간격(초)")
    sp.add_argument("--once", action="store_true", help="1회 소비 후 종료")
    sp.set_defaults(func=cmd_webhook_worker)

    sp = sub.add_parser("sync-products")
    sp.add_argument("--keyword", required=True)
    sp.add_argument("--pages", type=int, default=1)
    sp.set_defaults(func=cmd_sync_products)

    sp = sub.add_parser("sync-inventory"); sp.set_defaults(func=cmd_sync_inventory)

    sp = sub.add_parser("orders")
    osub = sp.add_subparsers(dest="sub", required=True)

    o = osub.add_parser("collect")
    o.add_argument("--channel", default="all", choices=["all", "woocommerce", "shopify"])
    o.set_defaults(func=cmd_orders_collect)

    o = osub.add_parser("preview")
    o.add_argument("--order-id", type=int, required=True)
    o.set_defaults(func=cmd_orders_preview)

    o = osub.add_parser("place")
    o.add_argument("--order-id", type=int, required=True)
    o.add_argument("--sandbox-first", action="store_true",
                   help="드라이런: 페이로드/마진 가드만 검증 (API 호출 없음)")
    o.add_argument("--confirm", action="store_true",
                   help="실주문 확정 플래그 (이 값 없으면 place_order 차단)")
    o.add_argument("--confirm-cj-sandbox", action="store_true",
                   help="CJ 샌드박스에 실제 주문 생성 (sandbox=True 경로)")
    o.set_defaults(func=cmd_orders_place)

    o = osub.add_parser("push-tracking")
    o.add_argument("--order-id", type=int, required=True)
    o.set_defaults(func=cmd_orders_tracking)

    sp = sub.add_parser("disputable")
    sp.add_argument("--order-id", type=int, required=True)
    sp.set_defaults(func=cmd_disputable)

    sp = sub.add_parser("dispute-create")
    sp.add_argument("--order-id", type=int, required=True)
    sp.add_argument("--line-id", type=int, required=True, help="cj_orders 행 ID")
    sp.add_argument("--kind", default="return", choices=["return", "refund"],
                    help="return=반품(기본), refund=환불 요청")
    sp.add_argument("--reason", required=True, help="분쟁 사유 텍스트")
    sp.add_argument("--amount", type=float, default=None,
                    help="요청 금액(미지정 시 행 단가×수량)")
    sp.add_argument("--confirm", action="store_true",
                    help="실제 CJ 분쟁 생성 (이 값 없으면 dry-run + 한도 검증만)")
    sp.set_defaults(func=cmd_dispute_create)

    sp = sub.add_parser("profit-report")
    sp.add_argument("--limit", type=int, default=200)
    sp.set_defaults(func=cmd_profit_report)

    sp = sub.add_parser("listing")
    sp.add_argument("action", choices=["publish", "hide"])
    sp.add_argument("--sku", required=True)
    sp.add_argument("--channels", default="shopify,woocommerce")
    sp.set_defaults(func=cmd_listing)

    sp = sub.add_parser("comments")
    csub = sp.add_subparsers(dest="sub", required=True)

    c = csub.add_parser("sync"); c.set_defaults(func=cmd_comments_sync)

    c = csub.add_parser("draft-reply")
    c.add_argument("--comment-id", type=int, required=True)
    c.add_argument("--body", required=True)
    c.set_defaults(func=cmd_comments_draft)

    c = csub.add_parser("send-reply")
    c.add_argument("--comment-id", type=int, required=True)
    c.set_defaults(func=cmd_comments_send)

    sp = sub.add_parser("audit-verify"); sp.set_defaults(func=cmd_audit_verify)
    sp = sub.add_parser("doctor"); sp.set_defaults(func=cmd_doctor)

    return p


def main():
    setup_logging()
    # P2-22: init_db() 를 인자 파싱 이후로 이동 — --help 등에서 불필요한 DB 생성 방지
    parser = build_parser()
    args = parser.parse_args()
    init_db()
    try:
        args.func(args)
    except Exception as e:
        log.exception("command failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
