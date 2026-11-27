import argparse
import contextlib
import os

from webapp.server import serve


def main() -> None:
    p = argparse.ArgumentParser(description="TradingAgents web app")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = p.parse_args()
    server = serve(args.host, args.port)
    print(f"TradingAgents web app on http://{args.host}:{args.port}")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
