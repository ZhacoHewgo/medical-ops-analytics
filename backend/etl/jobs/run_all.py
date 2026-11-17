"""Foreground extraction; explicit node 7 quality chain. No DDL or registration."""

import argparse
from datetime import date, datetime, timedelta
import json
import os
import re

from etl.scheduler import scheduler as scheduling


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise scheduling.EtlError("ETL_ARGUMENTS_INVALID")


def _validate_dates(start, end):
    if start is None and end is None:
        return
    try:
        for value in (start, end):
            if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError
            date.fromisoformat(value)
        if start > end:
            raise ValueError
    except ValueError:
        raise scheduling.EtlError("ETL_ARGUMENTS_INVALID") from None


def _extract_task(start, end, scheduled, *, through="extract"):
    def extract():
        # X6 can be developed independently; import only when a run actually starts.
        from etl.jobs.extract_ods import run_extract_all

        context = scheduling._CURRENT.get()
        if through == "quality":
            # Run context owns this identity: retries reuse it; next daily run cannot.
            if context is None:
                raise scheduling.EtlError("ETL_RUN_CONTEXT_MISSING")
            if context.pipeline is None:
                from etl.jobs.extract_ods import _freeze_sources
                try:
                    source_infos = _freeze_sources(None)
                except Exception as error:
                    from etl.config.source_config import OdsError
                    if isinstance(error, OdsError) and type(error.code) is str and error.code in scheduling._ODS_CODES:
                        raise OdsError(error.code) from None
                    raise OdsError("DWD_INTERNAL_ERROR") from None
                first_info = next(iter(source_infos.values()))
                context.pipeline = dict(source_infos=source_infos,
                                        dataset_id=first_info["dataset_id"],
                                        batch_date=first_info["etl_batch_date"],
                                        privacy_key_id=os.environ.get("DWD_PRIVACY_KEY_ID"))
        first, last = start, end
        if scheduled:
            context = scheduling._CURRENT.get()
            if context is None:
                raise scheduling.EtlError("ETL_RUN_CONTEXT_MISSING")
            if context.scheduled_date is None:
                yesterday = datetime.now(scheduling.SHANGHAI).date() - timedelta(days=1)
                context.scheduled_date = yesterday.isoformat()
            first = last = context.scheduled_date
        options = {"source_infos": context.pipeline["source_infos"]} if through == "quality" else {}
        result = run_extract_all(first, last, on_event=scheduling._source_event, **options)
        if (type(result) is not dict or result.keys() != scheduling.SOURCES.keys()
                or any(type(name) is not str for name in result)
                or any(type(value) is not int or value < 0 for value in result.values())):
            raise scheduling.EtlError("ETL_RESULT_INVALID")
        return result
    return extract


def _pipeline_identity(*, quality=False):
    from etl.config.source_config import OdsError
    from etl.jobs.extract_ods import _freeze_sources

    context = scheduling._CURRENT.get()
    if context is None or context.pipeline is None:
        raise scheduling.EtlError("ETL_RUN_CONTEXT_MISSING")
    identity = context.pipeline
    try:
        current = _freeze_sources(None)
    except OdsError as error:
        if type(error.code) is not str or error.code not in scheduling._ODS_CODES:
            raise
        raise scheduling.EtlError("DQ_SNAPSHOT_MISMATCH" if quality else "DWD_SOURCE_MISMATCH") from None
    if current != identity["source_infos"]:
        raise scheduling.EtlError("DQ_SNAPSHOT_MISMATCH" if quality else "DWD_SOURCE_MISMATCH")
    if os.environ.get("DWD_PRIVACY_KEY_ID") != identity["privacy_key_id"]:
        raise scheduling.EtlError("DWD_PRIVACY_KEY_CHANGED")
    return identity


def _new_error(error, fallback):
    # New-stage unknown errors must not enter node 6's generic retry fallback.
    from etl.config.source_config import OdsError

    if isinstance(error, (OdsError, scheduling.EtlError)):
        code = error.code
        if type(code) is str and code in scheduling._NODE07_CODES:
            return OdsError(code)
    return OdsError(fallback)


def _accepted_rows(tables, *, positive=False):
    from etl.jobs.clean_dwd import TABLES

    if type(tables) is not dict or tables.keys() != TABLES.keys():
        raise ValueError
    result = {}
    for table, entry in tables.items():
        if type(entry) is not dict:
            raise ValueError
        rows = entry.get("accepted_rows")
        if type(rows) is not int or rows < (1 if positive else 0):
            raise ValueError
        result[table] = rows
    return result


def _clean_task():
    try:
        from etl.jobs.clean_dwd import run_clean_all

        identity = _pipeline_identity()
        result = run_clean_all(identity["batch_date"], dataset_id=identity["dataset_id"])
        if (type(result) is not dict or result.get("dataset_id") != identity["dataset_id"]
                or result.get("batch_date") != identity["batch_date"]
                or type(result.get("clean_run_id")) is not str
                or not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", result["clean_run_id"])):
            raise ValueError
        counts = _accepted_rows(result.get("tables"))
        for table, entry in result["tables"].items():
            if entry.get("table") != table or any(entry.get(k) != result[k]
                    for k in ("dataset_id", "batch_date", "clean_run_id")):
                raise ValueError
        _pipeline_identity()
        identity["clean_run_id"] = result["clean_run_id"]
        identity["accepted_rows"] = counts
        return counts
    except Exception as error:
        raise _new_error(error, "DWD_INTERNAL_ERROR") from None


def _quality_task():
    try:
        from etl.jobs.quality_check import generate_quality_report

        identity = _pipeline_identity(quality=True)
        report = generate_quality_report(identity["batch_date"], dataset_id=identity["dataset_id"],
                                         env=scheduling._CURRENT.get().env)
        _pipeline_identity(quality=True)
        if (type(report) is not dict or any(report.get(k) != identity.get(k)
                for k in ("dataset_id", "batch_date", "clean_run_id", "privacy_key_id"))
                or "clean_run_id" not in identity
                or report.get("env") != scheduling._CURRENT.get().env):
            raise scheduling.EtlError("DQ_SNAPSHOT_MISMATCH")
        gate = report.get("gate_status")
        if gate == "FAIL":
            raise scheduling.EtlError("DQ_GATE_FAILED")
        if gate == "BLOCKED_INSUFFICIENT_EVIDENCE":
            raise scheduling.EtlError("DQ_INSUFFICIENT_EVIDENCE")
        if gate != "PASS":
            raise ValueError
        counts = _accepted_rows(report.get("tables"), positive=True)
        if counts != identity["accepted_rows"]:
            raise scheduling.EtlError("DQ_SNAPSHOT_MISMATCH")
        return counts
    except Exception as error:
        raise _new_error(error, "DQ_INTERNAL_ERROR") from None


def main(argv=None):
    try:
        parser = _SafeParser(description="Extraction with explicit quality chain and foreground scheduling")
        parser.add_argument("--start")
        parser.add_argument("--end")
        parser.add_argument("--env", choices=("dev", "test"), default="dev")
        parser.add_argument("--schedule", action="store_true")
        parser.add_argument("--through", choices=("extract", "quality"), default="extract")
        args = parser.parse_args(argv)
        _validate_dates(args.start, args.end)
        if args.schedule and args.start is not None:
            raise scheduling.EtlError("ETL_ARGUMENTS_INVALID")
        scheduling.register_task("extract_ods", _extract_task(args.start, args.end, args.schedule,
                                                           through=args.through))
        if args.through == "quality":
            scheduling.register_task("clean_dwd", _clean_task)
            scheduling.register_task("quality_check", _quality_task)
        if args.schedule:
            scheduler = scheduling.setup_schedule(env=args.env)
            try:
                scheduler.start()
            except KeyboardInterrupt:
                pass
            print(json.dumps(dict(code="ETL_OK", status="STOPPED")))
        else:
            result = scheduling.run_all(env=args.env)
            print(json.dumps(dict(code="ETL_OK", status="SUCCESS", tasks=result)))
        return 0
    except KeyboardInterrupt:
        print(json.dumps(dict(code="ETL_INTERRUPTED", status="FAILED")))
        return 130
    except Exception as error:
        print(json.dumps(dict(code=scheduling._safe_code(error), status="FAILED")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
