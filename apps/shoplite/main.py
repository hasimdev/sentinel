"""ShopLite: a tiny demo shop that Sentinel monitors (and breaks on purpose)."""

import secrets
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from apps.shoplite.metrics import instrument
from apps.shoplite.settings import Settings, load_settings
from common.logging_setup import configure_logging


class Product(BaseModel):
    id: str
    name: str
    price_cents: int


class CheckoutRequest(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1, le=10)


class CheckoutResponse(BaseModel):
    order_id: str
    product_id: str
    quantity: int
    total_cents: int


PRODUCTS = {
    p.id: p
    for p in [
        Product(id="mug", name="Coffee mug", price_cents=1200),
        Product(id="tee", name="T-shirt", price_cents=2500),
        Product(id="cap", name="Baseball cap", price_cents=1800),
        Product(id="sticker", name="Sticker pack", price_cents=500),
    ]
}

_rng = secrets.SystemRandom()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    log = configure_logging(settings.tags(), settings.log_file)
    app = FastAPI(title="ShopLite", version=settings.version)
    app.state.metrics_registry = instrument(app, settings.tags())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", **settings.tags()}

    @app.get("/products")
    def list_products() -> list[Product]:
        return list(PRODUCTS.values())

    @app.post("/checkout")
    def checkout(req: CheckoutRequest) -> CheckoutResponse:
        product = PRODUCTS.get(req.product_id)
        if product is None:
            log.warning("checkout rejected", extra={"fields": {"product_id": req.product_id}})
            raise HTTPException(status_code=404, detail="Unknown product")

        if _rng.random() < settings.fail_rate:
            log.error(
                "checkout failed: injected fault",
                extra={"fields": {"product_id": req.product_id, "fail_rate": settings.fail_rate}},
            )
            raise HTTPException(status_code=503, detail="Checkout temporarily unavailable")

        order = CheckoutResponse(
            order_id=uuid.uuid4().hex[:12],
            product_id=product.id,
            quantity=req.quantity,
            total_cents=product.price_cents * req.quantity,
        )
        log.info("checkout ok", extra={"fields": order.model_dump()})
        return order

    log.info("shoplite started", extra={"fields": {"fail_rate": settings.fail_rate}})
    return app


app = create_app()
