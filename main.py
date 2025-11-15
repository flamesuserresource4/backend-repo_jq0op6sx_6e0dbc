import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from bson.objectid import ObjectId

from database import db, create_document, get_documents
from schemas import Product, Cart, CartItem, Order, Review

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "E-commerce backend is running"}

# Utilities

def to_str_id(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    d = dict(doc)
    if d.get("_id"):
        d["id"] = str(d.pop("_id"))
    return d

# Seed some demo products if none exist
@app.post("/seed")
def seed_products():
    try:
        existing = db["product"].count_documents({}) if db else 0
        if existing == 0:
            demo = [
                Product(title="Echo Dot (5th Gen)", description="Smart speaker with Alexa", price=49.99, category="Electronics", image_url="https://images.unsplash.com/photo-1585386959984-a4155223168f", rating=4.4),
                Product(title="Noise Cancelling Headphones", description="Wireless over-ear", price=199.0, category="Electronics", image_url="https://images.unsplash.com/photo-1518443078004-7d3f3393815a", rating=4.5),
                Product(title="Stainless Steel Bottle", description="Insulated, 1L", price=19.99, category="Home & Kitchen", image_url="https://images.unsplash.com/photo-1602143407151-7111542de8f5", rating=4.2),
                Product(title="Running Shoes", description="Lightweight trainers", price=89.0, category="Fashion", image_url="https://images.unsplash.com/photo-1542291026-7eec264c27ff", rating=4.3),
                Product(title="Office Chair", description="Ergonomic mesh", price=159.0, category="Furniture", image_url="https://images.unsplash.com/photo-1582582494700-33a818b1d3c9", rating=4.1),
            ]
            for p in demo:
                create_document("product", p)
        return {"seeded": True, "count": int(db["product"].count_documents({}))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/categories")
def list_categories():
    try:
        cats = db["product"].distinct("category")
        return sorted([c for c in cats if c])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/products")
def list_products(
    category: Optional[str] = None,
    q: Optional[str] = None,
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    sort: Optional[str] = Query(None, description="price_asc|price_desc|rating_desc")
):
    try:
        filt: Dict[str, Any] = {}
        if category:
            filt["category"] = category
        if min_price is not None or max_price is not None:
            price_cond: Dict[str, Any] = {}
            if min_price is not None:
                price_cond["$gte"] = float(min_price)
            if max_price is not None:
                price_cond["$lte"] = float(max_price)
            filt["price"] = price_cond
        if q:
            # Use case-insensitive regex on title/description
            filt["$or"] = [
                {"title": {"$regex": q, "$options": "i"}},
                {"description": {"$regex": q, "$options": "i"}},
            ]

        cursor = db["product"].find(filt)
        if sort == "price_asc":
            cursor = cursor.sort("price", 1)
        elif sort == "price_desc":
            cursor = cursor.sort("price", -1)
        elif sort == "rating_desc":
            cursor = cursor.sort("rating", -1)

        products = [to_str_id(p) for p in cursor]
        return products
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/products/{product_id}")
def get_product(product_id: str):
    try:
        doc = db["product"].find_one({"_id": ObjectId(product_id)}) if ObjectId.is_valid(product_id) else db["product"].find_one({"id": product_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Product not found")
        prod = to_str_id(doc)
        # Aggregate reviews summary
        reviews = list(db["review"].find({"product_id": prod["id"]}))
        for r in reviews:
            r = r  # no-op
        count = len(reviews)
        if count:
            avg = sum(float(r.get("rating", 0)) for r in reviews) / count
            prod["rating"] = round(avg, 2)
            prod["reviews_count"] = count
        return prod
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class AddToCartRequest(BaseModel):
    product_id: str
    quantity: int = 1
    session_id: str

@app.post("/cart/add")
def add_to_cart(payload: AddToCartRequest):
    try:
        # Ensure product exists
        prod = None
        if ObjectId.is_valid(payload.product_id):
            prod = db["product"].find_one({"_id": ObjectId(payload.product_id)})
        if not prod:
            raise HTTPException(status_code=404, detail="Product not found")
        # Upsert a cart per session
        cart_col = db["cart"]
        cart = cart_col.find_one({"session_id": payload.session_id})
        if not cart:
            cart = {"session_id": payload.session_id, "items": []}
        # Add or update
        found = False
        for item in cart["items"]:
            if item["product_id"] == payload.product_id:
                item["quantity"] = max(1, min(10, int(item.get("quantity", 1)) + int(payload.quantity)))
                found = True
                break
        if not found:
            cart["items"].append({"product_id": payload.product_id, "quantity": max(1, min(10, int(payload.quantity)))})
        cart_col.update_one({"session_id": payload.session_id}, {"$set": cart}, upsert=True)
        return {"ok": True, "cart": cart}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cart")
def get_cart(session_id: str):
    try:
        cart = db["cart"].find_one({"session_id": session_id}) or {"session_id": session_id, "items": []}
        return cart
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class CheckoutRequest(BaseModel):
    session_id: str

@app.post("/checkout")
def checkout(payload: CheckoutRequest):
    try:
        cart = db["cart"].find_one({"session_id": payload.session_id})
        if not cart or not cart.get("items"):
            raise HTTPException(status_code=400, detail="Cart is empty")
        items = []
        total = 0.0
        for it in cart["items"]:
            prod = db["product"].find_one({"_id": ObjectId(it["product_id"])}) if ObjectId.is_valid(it["product_id"]) else None
            if not prod:
                continue
            qty = int(it.get("quantity", 1))
            price = float(prod.get("price", 0.0))
            total += price * qty
            items.append({"product_id": str(prod["_id"]), "quantity": qty, "price": price})
        if not items:
            raise HTTPException(status_code=400, detail="No valid items in cart")
        order = {"session_id": payload.session_id, "items": items, "total": round(total, 2), "status": "placed"}
        create_document("order", order)
        db["cart"].delete_one({"session_id": payload.session_id})
        return {"ok": True, "total": round(total, 2)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Reviews
class CreateReview(BaseModel):
    product_id: str
    user_name: str
    rating: float
    comment: Optional[str] = None

@app.get("/reviews")
def get_reviews(product_id: str):
    try:
        reviews = [to_str_id(r) for r in db["review"].find({"product_id": product_id}).sort("created_at", -1)]
        return reviews
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/reviews")
def post_review(payload: CreateReview):
    try:
        # Ensure product exists
        if not ObjectId.is_valid(payload.product_id) or not db["product"].find_one({"_id": ObjectId(payload.product_id)}):
            raise HTTPException(status_code=404, detail="Product not found")
        create_document("review", payload.model_dump())
        # Optionally update product rating lazily on read
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Orders history
@app.get("/orders")
def get_orders(session_id: str):
    try:
        orders = [to_str_id(o) for o in db["order"].find({"session_id": session_id}).sort("created_at", -1)]
        return orders
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    import os
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
