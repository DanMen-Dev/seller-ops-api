from fastapi import FastAPI

from app.api.routes import router

app = FastAPI(
    title="Seller Ops API",
    version="0.1.0",
    description="Stock alerts, unit margins per marketplace and PPC bid rules for Amazon sellers.",
)
app.include_router(router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}
