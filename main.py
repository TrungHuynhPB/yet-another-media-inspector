"""Run the Media Inspector web app."""

import uvicorn


def main():
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=["data", "data/*", "**/data/**"],
    )


if __name__ == "__main__":
    main()
