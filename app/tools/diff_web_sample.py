"""用显式固定身份启动单工作簿 Web Diff 本地验证服务。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse

from app.main import PROJECT_ROOT, create_app, load_config
from app.services.workbook_dataset_service import BoundWorkbookDatasetResolver
from core.svn_provider import MockSVNProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动固定数据集的 M2-05 Web 验证服务")
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "tests" / "excel" / "left")
    parser.add_argument("--target", type=Path, default=PROJECT_ROOT / "tests" / "excel" / "right")
    parser.add_argument("--workbook", default="AtlasConfig.xlsm")
    parser.add_argument("--source-endpoint-id", default="KR_FIX_KR-Fix-1.0.0.0")
    parser.add_argument("--source-revision", type=int, default=123456)
    parser.add_argument("--target-endpoint-id", default="KR_FIX_KR-Fix-1.0.1.0")
    parser.add_argument("--target-revision", type=int, default=123789)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5571)
    return parser


def create_sample_app(args):
    resolver = BoundWorkbookDatasetResolver(
        source_endpoint_id=args.source_endpoint_id,
        source_revision=args.source_revision,
        target_endpoint_id=args.target_endpoint_id,
        target_revision=args.target_revision,
        workbook_path=args.workbook,
        source_directory=args.source.resolve(),
        target_directory=args.target.resolve(),
    )
    application = create_app(
        config=load_config(),
        provider=MockSVNProvider(),
        workbook_dataset_resolver=resolver,
    )
    context = {
        "version": 1,
        "mode": "formal",
        "source": {
            "endpointId": args.source_endpoint_id,
            "label": args.source_endpoint_id,
            "branch": args.source_endpoint_id,
            "resolvedRevision": args.source_revision,
        },
        "target": {
            "endpointId": args.target_endpoint_id,
            "label": args.target_endpoint_id,
            "branch": args.target_endpoint_id,
            "resolvedRevision": args.target_revision,
        },
        "candidates": [
            {
                "path": args.workbook,
                "status": "modified",
                "sourceFile": {"path": args.workbook, "revision": args.source_revision},
                "targetFile": {"path": args.workbook, "revision": args.target_revision},
            }
        ],
        "results": [],
    }
    context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    context_literal = json.dumps(context_json, ensure_ascii=False).replace("<", r"\u003c")

    @application.get("/__local_verify/atlas", include_in_schema=False)
    def open_atlas_verification() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><meta charset=\"utf-8\"><script>"
            "sessionStorage.setItem(\"excelDiffTaskContext\","
            + context_literal
            + ");location.replace(\"/compare/results\");</script>"
        )

    return application


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    application = create_sample_app(args)
    uvicorn.run(application, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
