import asyncio
import sys

import uvicorn


def main():
    # Structured middleware logs paths without SSO codes or other query parameters.
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, loop="asyncio", access_log=False)
    server = uvicorn.Server(config)
    # Explicit factory: recent Uvicorn versions pick Proactor regardless of the global policy.
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(server.serve())


if __name__ == "__main__":
    main()
