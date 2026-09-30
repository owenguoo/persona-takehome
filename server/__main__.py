import argparse
import os
import sys

import uvicorn
from loguru import logger


def main():
    p = argparse.ArgumentParser(description="Onboarding server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5173)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    logger.remove()
    logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))
    print(f"onboarding → http://localhost:{args.port}", flush=True)
    uvicorn.run("server.app:app", host=args.host, port=args.port, reload=args.reload, log_level="warning")


if __name__ == "__main__":
    main()
