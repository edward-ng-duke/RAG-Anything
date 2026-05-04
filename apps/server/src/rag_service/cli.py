"""Console entry points: rag-api, rag-worker."""


def rag_api():
    import uvicorn

    uvicorn.run(
        "rag_service.api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


def rag_worker():
    from arq import run_worker
    from rag_service.worker.settings import WorkerSettings

    run_worker(WorkerSettings)
