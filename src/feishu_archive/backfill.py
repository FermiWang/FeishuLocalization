from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


BACKFILL_STATE_VERSION = 1
BACKFILL_ANALYSIS_MODE = "historical_backfill"


@dataclass(frozen=True)
class BackfillPolicy:
    timezone: str
    model: str
    start_hour: int
    end_hour: int
    minimum_idle_seconds: int

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if not self.model.strip():
            raise ValueError("回填模型不能为空")
        if not 0 <= self.start_hour <= 23:
            raise ValueError("回填开始小时必须在 0 到 23 之间")
        if not 1 <= self.end_hour <= 24:
            raise ValueError("回填结束小时必须在 1 到 24 之间")
        if self.start_hour >= self.end_hour:
            raise ValueError("回填运行窗口必须是同一自然日内的正向区间")
        if self.minimum_idle_seconds < 0:
            raise ValueError("最短空闲时间不能小于 0")


def within_backfill_window(now: datetime, policy: BackfillPolicy) -> bool:
    local = now.astimezone(ZoneInfo(policy.timezone))
    return policy.start_hour <= local.hour < policy.end_hour


def backfill_window_remaining_seconds(now: datetime, policy: BackfillPolicy) -> float:
    local = now.astimezone(ZoneInfo(policy.timezone))
    if not within_backfill_window(local, policy):
        return 0.0
    if policy.end_hour == 24:
        end = datetime.combine(
            local.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=local.tzinfo,
        )
    else:
        end = local.replace(
            hour=policy.end_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
    return max(0.0, (end - local).total_seconds())


def load_backfill_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("历史回填状态文件不可读") from exc
    if not isinstance(value, dict) or value.get("version") != BACKFILL_STATE_VERSION:
        raise ValueError("历史回填状态文件版本无效")
    _validate_state(value)
    return value


def ensure_backfill_state(
    path: Path,
    *,
    oldest_date: str,
    newest_date: str,
    timezone: str,
    model: str,
    prompt_version: str,
    analysis_config: Mapping[str, Any],
    archive_bounds: Mapping[str, Any],
    extend_newest: bool = False,
    now_ms: int | None = None,
) -> dict[str, Any]:
    oldest = date.fromisoformat(oldest_date)
    newest = date.fromisoformat(newest_date)
    now_ms = int(now_ms or time.time() * 1000)
    existing = load_backfill_state(path)
    identity = {
        "analysis_mode": BACKFILL_ANALYSIS_MODE,
        "direction": "forward",
        "timezone": timezone,
        "model": model,
        "prompt_version": prompt_version,
        "analysis_config_hash": hashlib.sha256(
            json.dumps(
                dict(analysis_config),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    same_identity = existing is not None and all(
        existing.get(key) == value for key, value in identity.items()
    )
    snapshot_audit_state_missing = bool(
        same_identity
        and (
            "source_snapshot_hashes" not in existing
            or "audit_next_date" not in existing
            or "resume_audit_date" not in existing
            or "projection_initialized" not in existing
            or "projection_reset_required" not in existing
            or "audit_cycles_completed" not in existing
        )
    )
    older_history_discovered = bool(
        same_identity
        and oldest < date.fromisoformat(str(existing["oldest_date"]))
    )
    if same_identity and not older_history_discovered and not snapshot_audit_state_missing:
        current_newest = date.fromisoformat(str(existing["newest_date"]))
        if extend_newest and newest > current_newest:
            # Extend in place without resetting the forward cursor. The worker
            # gains many dates per day while the boundary grows by at most one,
            # so a long campaign remains live and later daily failures cannot
            # create a permanent hole.
            existing["newest_date"] = newest.isoformat()
            if existing.get("next_date") is None:
                existing["resume_audit_date"] = (
                    existing.get("audit_next_date")
                    or existing.get("resume_audit_date")
                    or existing.get("oldest_date")
                )
                existing["next_date"] = (current_newest + timedelta(days=1)).isoformat()
                existing["status"] = "running"
                existing["audit_next_date"] = None
                existing["historical_backfill_complete"] = False
                existing["historical_analysis_complete"] = False
                existing["cumulative_ledger_complete"] = False
                existing["reconciliation_status"] = "in_progress"
                existing["finished_at"] = None
        existing["archive_bounds"] = dict(archive_bounds)
        existing["updated_at"] = now_ms
        _write_backfill_state(path, existing)
        return existing

    if existing is None:
        change_reason = "initial"
    elif snapshot_audit_state_missing:
        change_reason = "snapshot_audit_state_upgrade"
    elif older_history_discovered:
        change_reason = "older_history_discovered"
    else:
        change_reason = "analysis_identity_changed"
    state = _new_campaign_state(
        identity=identity,
        oldest=oldest,
        newest=newest,
        archive_bounds=archive_bounds,
        now_ms=now_ms,
        existing=existing,
        change_reason=change_reason,
    )
    _write_backfill_state(path, state)
    return state


def record_backfill_deferred(
    path: Path,
    state: Mapping[str, Any],
    *,
    reason: str,
    health: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    updated = dict(state)
    now_ms = int(now_ms or time.time() * 1000)
    updated.update(
        {
            "status": "deferred",
            "deferred_attempts": int(updated.get("deferred_attempts") or 0) + 1,
            "last_attempt_at": now_ms,
            "last_outcome": "deferred",
            "last_reason": str(reason),
            "last_health": dict(health or {}),
            "updated_at": now_ms,
        }
    )
    _write_backfill_state(path, updated)
    return updated


def record_backfill_error(
    path: Path,
    state: Mapping[str, Any],
    *,
    reason: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    updated = dict(state)
    now_ms = int(now_ms or time.time() * 1000)
    updated.update(
        {
            "status": "error",
            "error_attempts": int(updated.get("error_attempts") or 0) + 1,
            "last_attempt_at": now_ms,
            "last_outcome": "error",
            "last_reason": str(reason),
            "updated_at": now_ms,
        }
    )
    _write_backfill_state(path, updated)
    return updated


def record_backfill_success(
    path: Path,
    state: Mapping[str, Any],
    *,
    report_date: str,
    source_snapshot_hash: str,
    run_id: int | None,
    empty_day: bool,
    reused: bool = False,
    covered_by_daily: bool = False,
    health: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if report_date != state.get("next_date"):
        raise ValueError("回填完成日期与当前正序游标不一致")
    if state.get("projection_reset_required") or not state.get(
        "projection_initialized"
    ):
        raise ValueError("累计投影尚未按当前 campaign 初始化")
    snapshot_hash = str(source_snapshot_hash).strip()
    if not snapshot_hash:
        raise ValueError("回填完成日期缺少来源快照哈希")
    current = date.fromisoformat(report_date)
    newest = date.fromisoformat(str(state["newest_date"]))
    next_day = current + timedelta(days=1)
    complete = next_day > newest
    now_ms = int(now_ms or time.time() * 1000)
    updated = dict(state)
    source_snapshot_hashes = dict(updated.get("source_snapshot_hashes") or {})
    source_snapshot_hashes[report_date] = snapshot_hash
    updated.update(
        {
            "status": "auditing" if complete else "running",
            "next_date": None if complete else next_day.isoformat(),
            "audit_next_date": (
                str(
                    updated.get("resume_audit_date")
                    or updated["oldest_date"]
                )
                if complete
                else None
            ),
            "resume_audit_date": None if complete else updated.get(
                "resume_audit_date"
            ),
            "historical_backfill_complete": complete,
            "historical_analysis_complete": complete,
            # The cumulative projection is not declared complete until a
            # second oldest-to-newest pass confirms that no already-processed
            # date changed while the first pass was running.
            "cumulative_ledger_complete": False,
            "reconciliation_status": "audit_pending" if complete else "in_progress",
            "source_snapshot_hashes": source_snapshot_hashes,
            "processed_days": int(updated.get("processed_days") or 0) + 1,
            "successful_days": int(updated.get("successful_days") or 0) + 1,
            "empty_days": int(updated.get("empty_days") or 0) + int(empty_day),
            "daily_covered_days": int(updated.get("daily_covered_days") or 0)
            + int(covered_by_daily),
            "last_attempt_at": now_ms,
            "last_completed_at": now_ms,
            "last_report_date": report_date,
            "last_run_id": run_id,
            "last_outcome": (
                "daily_current_covered"
                if covered_by_daily
                else ("reused" if reused else ("empty" if empty_day else "success"))
            ),
            "last_reason": None,
            "last_health": dict(health or {}),
            "updated_at": now_ms,
            "analysis_finished_at": now_ms if complete else None,
            "finished_at": None,
        }
    )
    _write_backfill_state(path, updated)
    return updated


def mark_backfill_projection_initialized(
    path: Path,
    state: Mapping[str, Any],
    *,
    reset_summary: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Confirm that the current campaign's projection reset has committed.

    New and dirty campaigns set ``projection_reset_required``.  The CLI must
    reset the derived task/opportunity projection first, then call this helper
    *before* processing the oldest date.  Appending a normal upper-bound date
    preserves the already initialized projection and does not require a reset.
    """

    now_ms = int(now_ms or time.time() * 1000)
    updated = dict(state)
    updated.update(
        {
            "projection_initialized": True,
            "projection_reset_required": False,
            "projection_initialized_at": now_ms,
            "last_projection_reset": dict(reset_summary or {}),
            "updated_at": now_ms,
        }
    )
    _write_backfill_state(path, updated)
    return updated


def record_backfill_audit(
    path: Path,
    state: Mapping[str, Any],
    *,
    report_date: str,
    source_snapshot_hash: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Advance one strict-forward snapshot audit, or restart on a change.

    An audit never repairs a date in isolation.  If its current snapshot no
    longer matches the snapshot recorded by the analysis pass, a new campaign
    starts at ``oldest_date`` so task and opportunity projections are rebuilt
    in deterministic chronological order.
    """

    if state.get("next_date") is not None:
        raise ValueError("历史分析游标仍在运行，不能开始来源复核")
    if state.get("projection_reset_required") or not state.get(
        "projection_initialized"
    ):
        raise ValueError("累计投影尚未按当前 campaign 初始化")
    if report_date != state.get("audit_next_date"):
        raise ValueError("来源复核日期与当前正序复核游标不一致")
    snapshot_hash = str(source_snapshot_hash).strip()
    if not snapshot_hash:
        raise ValueError("来源复核缺少快照哈希")
    expected = str(
        (state.get("source_snapshot_hashes") or {}).get(report_date) or ""
    )
    now_ms = int(now_ms or time.time() * 1000)
    if expected != snapshot_hash:
        identity = {
            key: state[key]
            for key in (
                "analysis_mode",
                "direction",
                "timezone",
                "model",
                "prompt_version",
                "analysis_config_hash",
            )
        }
        source_change = {
            "report_date": report_date,
            "previous_hash": expected or None,
            "current_hash": snapshot_hash,
            "detected_at": now_ms,
        }
        restarted = _new_campaign_state(
            identity=identity,
            oldest=date.fromisoformat(str(state["oldest_date"])),
            newest=date.fromisoformat(str(state["newest_date"])),
            archive_bounds=dict(state.get("archive_bounds") or {}),
            now_ms=now_ms,
            existing=state,
            change_reason="source_snapshot_changed",
            change_details=source_change,
        )
        restarted.update(
            {
                "last_attempt_at": now_ms,
                "last_outcome": "source_snapshot_changed",
                "last_reason": f"source_snapshot_changed:{report_date}",
                "last_source_change": source_change,
            }
        )
        _write_backfill_state(path, restarted)
        return restarted

    current = date.fromisoformat(report_date)
    newest = date.fromisoformat(str(state["newest_date"]))
    next_day = current + timedelta(days=1)
    cycle_complete = next_day > newest
    updated = dict(state)
    projection_initialized = bool(updated.get("projection_initialized"))
    audit_cycles = int(updated.get("audit_cycles_completed") or 0)
    if cycle_complete:
        audit_cycles += 1
    cumulative_complete = bool(
        projection_initialized and (audit_cycles > 0 or updated.get("cumulative_ledger_complete"))
    )
    updated.update(
        {
            "status": "monitoring" if cumulative_complete else "auditing",
            # A completed pass immediately schedules the next oldest-to-newest
            # audit.  This is a continuous integrity loop, not a one-shot job.
            "audit_next_date": (
                str(updated["oldest_date"])
                if cycle_complete
                else next_day.isoformat()
            ),
            "audit_cycles_completed": audit_cycles,
            "audited_days": int(updated.get("audited_days") or 0) + 1,
            "last_audit_completed_at": now_ms if cycle_complete else updated.get(
                "last_audit_completed_at"
            ),
            "cumulative_ledger_complete": cumulative_complete,
            "reconciliation_status": (
                "complete" if cumulative_complete else "audit_in_progress"
            ),
            "last_attempt_at": now_ms,
            "last_completed_at": now_ms,
            "last_report_date": report_date,
            "last_outcome": "audit_cycle_complete" if cycle_complete else "audit_match",
            "last_reason": None,
            "updated_at": now_ms,
            "finished_at": now_ms if cumulative_complete else None,
        }
    )
    _write_backfill_state(path, updated)
    return updated


def evaluate_vmlx_load(
    health: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]],
    *,
    requested_model: str,
    minimum_idle_seconds: int,
    now_seconds: float | None = None,
) -> dict[str, Any]:
    now_seconds = float(time.time() if now_seconds is None else now_seconds)
    scheduler = health.get("scheduler")
    summary = {
        "status": health.get("status"),
        "model_loaded": health.get("model_loaded"),
        "model_name": health.get("model_name"),
        "num_running": scheduler.get("num_running") if isinstance(scheduler, Mapping) else None,
        "num_waiting": scheduler.get("num_waiting") if isinstance(scheduler, Mapping) else None,
        "last_request_time": health.get("last_request_time"),
    }

    def decision(state: str, reason: str, *, ready: bool = False) -> dict[str, Any]:
        return {"ready": ready, "state": state, "reason": reason, "summary": summary}

    if health.get("status") != "healthy" or health.get("model_loaded") is not True:
        return decision("unavailable", "vmlx_unhealthy_or_model_unloaded")
    if health.get("model_name") != requested_model:
        return decision("unavailable", "vmlx_health_model_mismatch")
    model_ids = {str(item.get("id") or "") for item in models if isinstance(item, Mapping)}
    if requested_model not in model_ids:
        return decision("unavailable", "vmlx_models_mismatch")
    if not isinstance(scheduler, Mapping):
        return decision("unknown", "vmlx_scheduler_missing")
    running = scheduler.get("num_running")
    waiting = scheduler.get("num_waiting")
    if (
        isinstance(running, bool)
        or isinstance(waiting, bool)
        or not isinstance(running, (int, float))
        or not isinstance(waiting, (int, float))
    ):
        return decision("unknown", "vmlx_scheduler_invalid")
    try:
        running_value = float(running)
        waiting_value = float(waiting)
    except (TypeError, ValueError, OverflowError):
        return decision("unknown", "vmlx_scheduler_invalid")
    if (
        not math.isfinite(running_value)
        or not math.isfinite(waiting_value)
        or running_value < 0
        or waiting_value < 0
    ):
        return decision("unknown", "vmlx_scheduler_invalid")
    if running_value > 0 or waiting_value > 0:
        return decision("busy", "vmlx_scheduler_busy")
    if "last_request_time" not in health:
        return decision("unknown", "vmlx_last_request_missing")
    last_request = health["last_request_time"]
    if last_request is None:
        return decision("unknown", "vmlx_last_request_uninitialized")
    if isinstance(last_request, bool) or not isinstance(last_request, (int, float)):
        return decision("unknown", "vmlx_last_request_invalid")
    try:
        idle_seconds = now_seconds - float(last_request)
    except (TypeError, ValueError, OverflowError):
        return decision("unknown", "vmlx_last_request_invalid")
    if not math.isfinite(idle_seconds) or idle_seconds < -5:
        return decision("unknown", "vmlx_clock_skew")
    summary["idle_seconds"] = max(0.0, idle_seconds)
    if idle_seconds < minimum_idle_seconds:
        return decision("cooldown", "vmlx_idle_cooldown")
    return decision("idle", "vmlx_idle", ready=True)


def public_backfill_status(path: Path) -> dict[str, Any]:
    state = load_backfill_state(path)
    if state is None:
        return {"configured": False, "status": "not_started"}
    return {"configured": True, **state}


def _new_campaign_state(
    *,
    identity: Mapping[str, Any],
    oldest: date,
    newest: date,
    archive_bounds: Mapping[str, Any],
    now_ms: int,
    existing: Mapping[str, Any] | None,
    change_reason: str,
    change_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    previous: list[dict[str, Any]] = []
    if existing is not None:
        previous = list(existing.get("previous_campaigns") or [])
        archived = {
            "campaign_id": existing.get("campaign_id"),
            "status": existing.get("status"),
            "oldest_date": existing.get("oldest_date"),
            "newest_date": existing.get("newest_date"),
            "next_date": existing.get("next_date"),
            "audit_next_date": existing.get("audit_next_date"),
            "resume_audit_date": existing.get("resume_audit_date"),
            "processed_days": existing.get("processed_days", 0),
            "audited_days": existing.get("audited_days", 0),
            "audit_cycles_completed": existing.get("audit_cycles_completed", 0),
            "projection_initialized": existing.get("projection_initialized", False),
            "projection_reset_required": existing.get(
                "projection_reset_required", True
            ),
            "model": existing.get("model"),
            "prompt_version": existing.get("prompt_version"),
            "analysis_config_hash": existing.get("analysis_config_hash"),
            "updated_at": existing.get("updated_at"),
            "superseded_reason": change_reason,
        }
        if change_details:
            archived["change_details"] = dict(change_details)
        previous.append(archived)
        previous = previous[-20:]

    has_work = oldest <= newest
    seed_value: dict[str, Any] = {
        **dict(identity),
        "oldest_date": oldest.isoformat(),
        "newest_date": newest.isoformat(),
        "created_at": now_ms,
        "change_reason": change_reason,
    }
    if change_details:
        seed_value["change_details"] = dict(change_details)
    campaign_seed = json.dumps(
        seed_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "version": BACKFILL_STATE_VERSION,
        "campaign_id": hashlib.sha256(campaign_seed.encode("utf-8")).hexdigest(),
        **dict(identity),
        "campaign_change_reason": change_reason,
        "oldest_date": oldest.isoformat(),
        "newest_date": newest.isoformat(),
        "next_date": oldest.isoformat() if has_work else None,
        "audit_next_date": None,
        "resume_audit_date": None,
        "status": "running" if has_work else "monitoring",
        "historical_backfill_complete": not has_work,
        "historical_analysis_complete": not has_work,
        "projection_initialized": not has_work,
        "projection_reset_required": has_work,
        "projection_initialized_at": now_ms if not has_work else None,
        "cumulative_ledger_complete": not has_work,
        "reconciliation_status": "in_progress" if has_work else "complete",
        "source_snapshot_hashes": {},
        "processed_days": 0,
        "successful_days": 0,
        "empty_days": 0,
        "daily_covered_days": 0,
        "audited_days": 0,
        "audit_cycles_completed": 0,
        "deferred_attempts": 0,
        "error_attempts": 0,
        "last_attempt_at": None,
        "last_completed_at": None,
        "last_audit_completed_at": None,
        "last_report_date": None,
        "last_run_id": None,
        "last_outcome": None,
        "last_reason": None,
        "last_health": None,
        "archive_bounds": dict(archive_bounds),
        "created_at": now_ms,
        "updated_at": now_ms,
        "analysis_finished_at": now_ms if not has_work else None,
        "finished_at": now_ms if not has_work else None,
        "previous_campaigns": previous,
    }


def _validate_state(value: Mapping[str, Any]) -> None:
    if value.get("direction") not in {"forward", "reverse"} or value.get(
        "analysis_mode"
    ) != BACKFILL_ANALYSIS_MODE:
        raise ValueError("历史回填状态方向或模式无效")
    oldest = date.fromisoformat(str(value.get("oldest_date") or ""))
    newest = date.fromisoformat(str(value.get("newest_date") or ""))
    next_value = value.get("next_date")
    if next_value is not None:
        next_day = date.fromisoformat(str(next_value))
        if not oldest <= next_day <= newest:
            raise ValueError("历史回填游标越界")
    audit_value = value.get("audit_next_date")
    if audit_value is not None:
        audit_day = date.fromisoformat(str(audit_value))
        if not oldest <= audit_day <= newest:
            raise ValueError("历史回填复核游标越界")
        if next_value is not None:
            raise ValueError("历史分析和复核游标不能同时运行")
    resume_audit_value = value.get("resume_audit_date")
    if resume_audit_value is not None:
        resume_audit_day = date.fromisoformat(str(resume_audit_value))
        if not oldest <= resume_audit_day <= newest:
            raise ValueError("历史回填待恢复复核游标越界")
        if next_value is None or audit_value is not None:
            raise ValueError("待恢复复核游标只能与历史分析游标同时存在")
    snapshots = value.get("source_snapshot_hashes")
    if snapshots is not None:
        if not isinstance(snapshots, Mapping):
            raise ValueError("历史回填来源快照状态无效")
        for snapshot_date, snapshot_hash in snapshots.items():
            parsed = date.fromisoformat(str(snapshot_date))
            if not oldest <= parsed <= newest or not str(snapshot_hash).strip():
                raise ValueError("历史回填来源快照条目无效")
    if value.get("cumulative_ledger_complete") and (
        not value.get("projection_initialized", True)
        or value.get("projection_reset_required", False)
    ):
        raise ValueError("累计台账已完成但投影尚未初始化")
    if value.get("status") in {"complete", "analysis_complete"} and next_value is not None:
        raise ValueError("已完成的历史回填仍包含游标")


def _write_backfill_state(path: Path, value: Mapping[str, Any]) -> None:
    _validate_state(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(encoded, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
