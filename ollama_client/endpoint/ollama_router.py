from fastapi import APIRouter, Request, HTTPException
from fastapi.concurrency import run_in_threadpool
from ollama_client.endpoint.ollama_entities import (
    ChatRequest,
    ChatResponse,
    ModelsResponse,
    OllamaHealthResponse,
)
from ollama_client.endpoint.ollama_lifespan import ollama_lifespan
from ollama_client.endpoint.ollama_settings import get_ollama_settings

ollama_router = APIRouter(
    prefix="/ollama",
    tags=["Ollama"],
    lifespan=ollama_lifespan,
)


def _get_client(req: Request):
    client = getattr(req.app.state, "ollama_client", None)
    if client is None:
        raise HTTPException(status_code=500, detail="Ollama client not initialized")
    return client


@ollama_router.get("/health", response_model=OllamaHealthResponse)
async def ollama_health(req: Request):
    client = _get_client(req)
    settings = get_ollama_settings()
    if not await run_in_threadpool(client.connect):
        raise HTTPException(status_code=503, detail="Ollama unavailable")
    return OllamaHealthResponse(
        status="ok",
        url=settings.ollama_url,
        model=settings.model_name,
        model_available=await run_in_threadpool(client.has_model),
    )


@ollama_router.get("/models", response_model=ModelsResponse)
async def ollama_models(req: Request):
    client = _get_client(req)
    return ModelsResponse(
        default_model=get_ollama_settings().model_name,
        models=await run_in_threadpool(client.list_models),
    )


@ollama_router.post("/chat", response_model=ChatResponse)
async def chat_with_ollama(request: ChatRequest, req: Request):
    client = _get_client(req)
    if not client.is_connected and not await run_in_threadpool(client.connect):
        raise HTTPException(status_code=503, detail="Ollama unavailable")

    settings = get_ollama_settings()
    try:
        # query() синхронный (requests): в threadpool, чтобы не блокировать event loop
        response_text = await run_in_threadpool(
            client.query,
            instruction=request.instruction,
            prompt=request.prompt,
            temperature=settings.temperature if request.temperature is None else request.temperature,
            max_tokens=request.max_tokens or settings.max_tokens,
            model_name=request.model_name,
        )
        return ChatResponse(response=response_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
