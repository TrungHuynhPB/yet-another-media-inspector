"""Run the Media Inspector web app."""

import sys

import uvicorn
from dotenv import load_dotenv

load_dotenv()


def main():
    from media import opencv_diagnostics

    diag = opencv_diagnostics()
    print(f"YAMI Python: {diag['pythonExecutable']}")
    if diag.get("opencvAvailable"):
        print(f"OpenCV {diag.get('opencvVersion', '')} OK")
    else:
        print(f"WARNING: {diag.get('error', 'OpenCV not available')}")
        print(f"  Fix: {sys.executable} -m pip install opencv-python")

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=["data", "data/*", "**/data/**"],
    )


if __name__ == "__main__":
    main()
