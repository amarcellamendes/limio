from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os

from .database import init_db
from .routers import auth_router, clientes_router, notas_router, recebidos_router, calendario_router, contratos_router, apuracao_router, usuarios_router, integracoes_router, certidoes_router, fornecedores_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Limio",
    description="SaaS multi-tenant para emissão de NFS-e e NF-e",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://limio.com.br", "https://www.limio.com.br", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(clientes_router.router)
app.include_router(notas_router.router)
app.include_router(recebidos_router.router)
app.include_router(calendario_router.router)
app.include_router(contratos_router.router)
app.include_router(apuracao_router.router)
app.include_router(usuarios_router.router)
app.include_router(integracoes_router.router)
app.include_router(certidoes_router.router)
app.include_router(fornecedores_router.router)

# Serve o frontend a partir de /frontend
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
_frontend_dir = os.path.abspath(_frontend_dir)

if os.path.isdir(_frontend_dir):
    _static_dir = os.path.join(_frontend_dir, "static")
    if os.path.isdir(_static_dir):
        app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str = ""):
        # Não intercepta rotas da API
        if full_path.startswith("api/"):
            from fastapi import HTTPException
            raise HTTPException(404)
        index = os.path.join(_frontend_dir, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return {"detail": "Frontend não encontrado. Coloque index.html em /frontend/"}
