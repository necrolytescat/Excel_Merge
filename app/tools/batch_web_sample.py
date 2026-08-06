"""启动固定 AtlasConfig 数据集的 M2-07 批量 Web 验收服务。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from fastapi.responses import HTMLResponse
import uvicorn

from app.main import PROJECT_ROOT, create_app, load_config
from app.schemas.batch import (
    BatchCandidatePayload,
    BatchCandidateSidePayload,
    BatchCreateRequestPayload,
    BatchEndpointPayload,
)
from app.services.batch_diff_service import BatchDiffService, DefaultBatchWorkbookRunner
from app.services.batch_store import BatchDiffError, BatchStore, json_hash
from app.services.workbook_dataset_service import BoundWorkbookDatasetResolver
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from core.svn_provider import MockSVNProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动固定数据集的 M2-07 批量 Web 验收服务")
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "tests" / "excel" / "left")
    parser.add_argument("--target", type=Path, default=PROJECT_ROOT / "tests" / "excel" / "right")
    parser.add_argument("--workbook", default="AtlasConfig.xlsm")
    parser.add_argument("--source-endpoint-id", default="KR_FIX_KR-Fix-1.0.0.0")
    parser.add_argument("--source-revision", type=int, default=123456)
    parser.add_argument("--target-endpoint-id", default="KR_FIX_KR-Fix-1.0.1.0")
    parser.add_argument("--target-revision", type=int, default=123789)
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=PROJECT_ROOT / "var" / "m2-batch-sample",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5572)
    return parser


class AtlasCandidateResolver:
    def __init__(self, args):
        self.args = args

    def validate_endpoints(self, source, target) -> None:
        expected = (
            (source, self.args.source_endpoint_id, self.args.source_revision),
            (target, self.args.target_endpoint_id, self.args.target_revision),
        )
        for endpoint, endpoint_id, revision in expected:
            if endpoint.endpoint_id != endpoint_id or endpoint.revision != revision:
                raise BatchDiffError(
                    "BATCH_SNAPSHOT_CONTEXT_MISMATCH",
                    "本地样例仅接受预设的固定端点与 Revision",
                    status_code=409,
                )

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def prepare(self, source, target):
        self.validate_endpoints(source, target)
        source_path = self.args.source / self.args.workbook
        target_path = self.args.target / self.args.workbook
        source_side = BatchCandidateSidePayload(
            exists=True,
            size_bytes=source_path.stat().st_size,
            content_sha256=self._digest(source_path),
        )
        target_side = BatchCandidateSidePayload(
            exists=True,
            size_bytes=target_path.stat().st_size,
            content_sha256=self._digest(target_path),
        )
        facts = {
            "path": self.args.workbook,
            "status": "modified",
            "source": source_side.model_dump(mode="json"),
            "target": target_side.model_dump(mode="json"),
        }
        return [BatchCandidatePayload(**facts, fingerprint_sha256=json_hash(facts))]


def create_sample_app(args):
    source_directory = args.source.resolve()
    target_directory = args.target.resolve()
    config = load_config()
    layout = DatasetLayout.from_config(config["dataset_layout"])
    diff_service = WorkbookDiffService(layout)
    dataset_resolver = BoundWorkbookDatasetResolver(
        source_endpoint_id=args.source_endpoint_id,
        source_revision=args.source_revision,
        target_endpoint_id=args.target_endpoint_id,
        target_revision=args.target_revision,
        workbook_path=args.workbook,
        source_directory=source_directory,
        target_directory=target_directory,
    )
    batch_service = BatchDiffService(
        BatchStore(args.state_directory.resolve()),
        AtlasCandidateResolver(args),
        DefaultBatchWorkbookRunner(dataset_resolver, diff_service),
    )
    application = create_app(
        config=config,
        provider=MockSVNProvider(),
        workbook_dataset_resolver=dataset_resolver,
        workbook_diff_service=diff_service,
        batch_diff_service=batch_service,
    )

    @application.get("/__local_verify/atlas-batch", include_in_schema=False)
    def open_atlas_batch_verification() -> HTMLResponse:
        task, _ = batch_service.create_task(
            BatchCreateRequestPayload(
                schema_version="m2.batch-create.request.v1",
                request_id=uuid4(),
                source=BatchEndpointPayload(
                    endpoint_id=args.source_endpoint_id,
                    revision=args.source_revision,
                ),
                target=BatchEndpointPayload(
                    endpoint_id=args.target_endpoint_id,
                    revision=args.target_revision,
                ),
            )
        )
        context = {
            "version": 2,
            "mode": "formal",
            "batchTaskId": str(task.task_id),
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
            "candidates": [],
            "results": [],
        }
        context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        context_literal = json.dumps(context_json, ensure_ascii=False).replace("<", r"\u003c")
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
