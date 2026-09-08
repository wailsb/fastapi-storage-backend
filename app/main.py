import os
from fastapi import FastAPI, HTTPException, status
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://storage_admin:storage_password_123@db:5432/cloud_storage_db")

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI(
    title="Resumable Cloud Storage Engine",
    version="1.0.0",
    description="FastAPI service supporting tus protocol uploads, authentication, and audit tracking."
)

@app.get("/health")
async def health_check():
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(text("SELECT 1"))
            return {"status": "healthy", "database": "connected"}
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database connection error: {str(e)}"
            )

@app.get("/")
def read_root():
    return {"system": "Resumable Upload Storage API", "status": "running"}