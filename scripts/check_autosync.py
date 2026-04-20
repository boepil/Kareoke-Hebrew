from __future__ import annotations

import argparse
import importlib
import json
import platform
import sys
from pathlib import Path


def _module_status(name: str) -> dict[str, object]:
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "version": getattr(module, "__version__", "unknown"),
    }


def build_report() -> dict[str, object]:
    report: dict[str, object] = {
        "python": sys.version,
        "platform": platform.platform(),
        "modules": {
            "torch": _module_status("torch"),
            "transformers": _module_status("transformers"),
            "accelerate": _module_status("accelerate"),
            "timm": _module_status("timm"),
            "librosa": _module_status("librosa"),
            "soundfile": _module_status("soundfile"),
            "demucs": _module_status("demucs"),
            "whisperx": _module_status("whisperx"),
        },
    }

    try:
        import torch  # type: ignore

        report["torch_runtime"] = {
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
            "cuda_version": getattr(torch.version, "cuda", None),
        }
    except Exception as exc:  # pragma: no cover - environment dependent
        report["torch_runtime"] = {"error": f"{type(exc).__name__}: {exc}"}

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the optional autosync environment.")
    parser.add_argument("--output", help="Write the report to this JSON path.")
    args = parser.parse_args()

    report = build_report()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
