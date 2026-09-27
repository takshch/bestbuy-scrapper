from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import PriceHistory, Product
from app.schemas import PricePoint, ProductOut, ProductPage

router = APIRouter(prefix="/products", tags=["products"])

SORTS = {
    "updated": Product.updated_at.desc(),
    "price_asc": Product.price.asc().nulls_last(),
    "price_desc": Product.price.desc().nulls_last(),
    "rating": Product.rating.desc().nulls_last(),
}


@router.get("", response_model=ProductPage)
async def list_products(
    q: Optional[str] = Query(None, description="Search in product name"),
    search_query: Optional[str] = Query(None, description="Filter by the scrape query that found it"),
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    sort: str = Query("updated", enum=list(SORTS)),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Product)
    if q:
        stmt = stmt.where(Product.name.ilike(f"%{q}%"))
    if search_query:
        stmt = stmt.where(Product.search_query == search_query)
    if min_price is not None:
        stmt = stmt.where(Product.price >= min_price)
    if max_price is not None:
        stmt = stmt.where(Product.price <= max_price)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    res = await session.execute(stmt.order_by(SORTS[sort]).limit(limit).offset(offset))
    return ProductPage(total=total or 0, limit=limit, offset=offset, items=res.scalars().all())


@router.get("/{sku}", response_model=ProductOut)
async def get_product(sku: str, session: AsyncSession = Depends(get_session)):
    product = await session.scalar(select(Product).where(Product.sku == sku))
    if not product:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


@router.get("/{sku}/history", response_model=List[PricePoint])
async def price_history(sku: str, session: AsyncSession = Depends(get_session)):
    """Price changes observed for a product, oldest first."""
    if not await session.scalar(select(Product.id).where(Product.sku == sku)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    stmt = select(PriceHistory).where(PriceHistory.sku == sku).order_by(PriceHistory.observed_at)
    return (await session.execute(stmt)).scalars().all()
