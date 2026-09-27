"""Persisting scraped products and their price history."""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceHistory, Product

PRODUCT_FIELDS = (
    "name", "model", "url", "image_url", "price", "regular_price",
    "rating", "review_count", "availability",
)


async def save_products(
    session: AsyncSession, products: List[Dict[str, Any]], query: str, job_id: Optional[int] = None
) -> int:
    """Upsert products and record a price-history row for each new SKU or changed price.

    Runs inside the caller's transaction. Returns the number of products saved.
    """
    rows = {}
    for p in products:
        if p.get("sku") and p.get("name"):
            rows[str(p["sku"])] = {"sku": str(p["sku"]), "search_query": query,
                                   **{f: p.get(f) for f in PRODUCT_FIELDS}}
    if not rows:
        return 0
    # Consistent lock order across concurrent workers upserting overlapping SKUs (avoids deadlocks).
    ordered = [rows[sku] for sku in sorted(rows)]

    existing = {
        sku: (price, regular)
        for sku, price, regular in (
            await session.execute(
                select(Product.sku, Product.price, Product.regular_price).where(Product.sku.in_(rows))
            )
        ).all()
    }

    stmt = insert(Product).values(ordered)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[Product.sku],
        set_={
            # A degraded page may lack some fields: keep what we already know.
            **{f: func.coalesce(getattr(excluded, f), getattr(Product, f)) for f in PRODUCT_FIELDS},
            "search_query": excluded.search_query,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    await session.execute(stmt)

    history = [
        {"sku": r["sku"], "price": r["price"], "regular_price": r["regular_price"], "job_id": job_id}
        for r in ordered
        if r["price"] is not None
        and (r["sku"] not in existing or existing[r["sku"]] != (r["price"], r["regular_price"]))
    ]
    if history:
        await session.execute(insert(PriceHistory).values(history))
    return len(ordered)
