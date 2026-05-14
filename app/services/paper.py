from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CollectionRun, PaperTrade, PriceSnapshot, StrategyDecision
from app.services.entry_plan import entry_plan_compatibility, entry_plan_is_expired
from app.services.risk import template_for_tier
from app.services.strategy import evaluate_symbol
from app.services.signal_attribution import backfill_signal_outcomes
from app.services.telegram import TelegramClient


RUNNER_TIERS = {"high_volatility"}
RUNNER_SYMBOLS = {"DOGEUSDT", "HYPEUSDT", "ZECUSDT"}
RUNNER_LOCK_FRACTION = 0.45
RUNNER_EXTENSION_FRACTION = 0.35
RUNNER_MIN_EXTENSION_PCT = 0.012
RUNNER_EVIDENCE_MAX_AGE_MINUTES = 120
RUNNER_OPENING_EVIDENCE_MAX_AGE_MINUTES = 24 * 60
RUNNER_EVIDENCE_MIN_SCORE = 5.0


@dataclass
class PaperScanResult:
    opened: list[dict[str, Any]] = field(default_factory=list)
    observed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


async def paper_scan(
    session: AsyncSession,
    coins: list[str],
    dry_run: bool = False,
    notify: bool = False,
) -> PaperScanResult:
    result = PaperScanResult()
    collection_run = await _latest_completed_collection_run(session)
    scan_context = _scan_context(collection_run)
    collection_run_id = scan_context.get("collection_run_id")
    for coin in coins:
        symbol = f"{coin.upper()}USDT"
        if collection_run_id:
            existing_scan = await _paper_scan_for_collection(session, symbol, str(collection_run_id))
            if existing_scan:
                result.skipped.append(
                    {
                        "symbol": symbol,
                        "reason": "collection_already_scanned",
                        "collection_run_id": collection_run_id,
                        "decision_id": existing_scan.id,
                    }
                )
                continue
        # Paper scans should record decision snapshots even when they do not open trades.
        # This creates the history needed for consecutive-signal confirmation.
        evaluation = await evaluate_symbol(session, coin, dry_run=False)
        latest_decision = await _latest_decision(session, evaluation.symbol)
        await _record_paper_scan_status(
            session,
            latest_decision,
            confirmation={"required": 2, "streak": 0, "confirmed": False, "label": "等待连续确认"},
            status="evaluated",
            scan_context=scan_context,
        )
        confirmation = await _confirmation_for_symbol(
            session,
            evaluation.symbol,
            evaluation.direction,
            current_risk=evaluation.risk_payload,
        )
        evaluation.risk_payload["confirmation"] = confirmation
        evaluation.risk_payload["paper_scan_context"] = scan_context
        if evaluation.decision != "open":
            await _record_paper_scan_status(
                session,
                latest_decision,
                confirmation=confirmation,
                status="observed",
                scan_context=scan_context,
            )
            result.observed.append(_evaluation_payload(evaluation))
            continue
        if not confirmation["confirmed"]:
            payload = _evaluation_payload(evaluation)
            payload["decision"] = "observe"
            payload["paper_scan_status"] = "awaiting_confirmation"
            await _record_paper_scan_status(
                session,
                latest_decision,
                confirmation=confirmation,
                status="awaiting_confirmation",
                scan_context=scan_context,
            )
            result.observed.append(payload)
            if notify:
                await _notify_safe(_format_candidate_message(payload))
            continue
        existing = await _open_trade_for_symbol(session, evaluation.symbol)
        if existing:
            await _record_paper_scan_status(
                session,
                latest_decision,
                confirmation=confirmation,
                status="open_trade_exists",
                scan_context=scan_context,
            )
            result.skipped.append(
                {
                    "symbol": evaluation.symbol,
                    "reason": "open_trade_exists",
                    "trade_id": existing.id,
                }
            )
            continue
        if dry_run:
            payload = _evaluation_payload(evaluation)
            result.opened.append(payload)
            if notify:
                await _notify_safe(_format_candidate_message(payload))
            continue

        decision = latest_decision
        if decision is None:
            result.skipped.append({"symbol": evaluation.symbol, "reason": "missing_decision"})
            continue
        trade = PaperTrade(
            strategy_decision_id=decision.id,
            strategy_name=decision.strategy_name,
            strategy_version=decision.strategy_version,
            symbol=evaluation.symbol,
            asset_tier=evaluation.asset_tier,
            direction=evaluation.direction,
            entry_price=evaluation.risk_payload["entry_price"],
            stop_loss=evaluation.risk_payload["stop_loss"],
            take_profit=evaluation.risk_payload["take_profit"],
            position_size=evaluation.risk_payload["risk_fraction"],
        )
        session.add(trade)
        await _record_paper_scan_status(
            session,
            decision,
            confirmation=confirmation,
            status="opened",
            scan_context=scan_context,
            commit=False,
        )
        await session.commit()
        payload = {"symbol": trade.symbol, "trade_id": trade.id, "journal": _journal(decision)}
        result.opened.append(payload)
        if notify:
            await _notify_safe(_format_open_message(trade, decision))
    return result


async def mark_open_trades(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(PaperTrade).where(PaperTrade.status == "open").order_by(PaperTrade.opened_at)
    )
    trades = rows.scalars().all()
    closed: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for trade in trades:
        price = await _latest_price(session, trade.symbol)
        if price is None:
            continue
        pnl = _pnl(trade.direction, trade.entry_price, price)
        trade.mfe = max(trade.mfe, pnl)
        trade.mae = min(trade.mae, pnl)
        previous_stop_loss = trade.stop_loss
        previous_take_profit = trade.take_profit
        exit_reason = _stop_exit_reason(trade, price)
        runner_decision: dict[str, Any] | None = None
        if exit_reason is None and _take_profit_touched(trade, price):
            runner_decision = await _take_profit_runner_decision(session, trade, price)
            if runner_decision["extend"]:
                _extend_take_profit_runner(trade, price)
            else:
                exit_reason = "take_profit"
        if exit_reason:
            trade.status = "closed"
            trade.exit_price = price
            trade.exit_reason = exit_reason
            trade.pnl = pnl * trade.position_size
            stop_pct = abs(trade.entry_price - trade.stop_loss) / trade.entry_price
            trade.r_multiple = pnl / stop_pct if stop_pct else 0.0
            trade.closed_at = datetime.now(timezone.utc)
            closed.append({"trade_id": trade.id, "symbol": trade.symbol, "reason": exit_reason, "pnl_pct": pnl})
            await _notify_safe(_format_close_message(trade, pnl, exit_reason))
        else:
            payload = {"trade_id": trade.id, "symbol": trade.symbol, "pnl_pct": pnl}
            if trade.stop_loss != previous_stop_loss or trade.take_profit != previous_take_profit:
                payload.update(
                    {
                        "runner_extended": True,
                        "runner_evidence": runner_decision,
                        "stop_loss": trade.stop_loss,
                        "take_profit": trade.take_profit,
                        "previous_stop_loss": previous_stop_loss,
                        "previous_take_profit": previous_take_profit,
                    }
                )
            updated.append(payload)
    await session.commit()
    attribution = await backfill_signal_outcomes(session, limit=500)
    return {"closed": closed, "updated": updated, "attribution": attribution.__dict__}


async def _latest_price(session: AsyncSession, symbol: str) -> float | None:
    rows = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.symbol == symbol)
        .order_by(PriceSnapshot.created_at.desc())
        .limit(1)
    )
    item = rows.scalar_one_or_none()
    return item.price if item else None


async def _open_trade_for_symbol(session: AsyncSession, symbol: str) -> PaperTrade | None:
    rows = await session.execute(
        select(PaperTrade)
        .where(PaperTrade.symbol == symbol, PaperTrade.status == "open")
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _latest_completed_collection_run(session: AsyncSession) -> CollectionRun | None:
    rows = await session.execute(
        select(CollectionRun)
        .where(CollectionRun.status == "completed", CollectionRun.dry_run.is_(False))
        .order_by(CollectionRun.finished_at.desc(), CollectionRun.started_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


def _scan_context(collection_run: CollectionRun | None) -> dict[str, Any]:
    if collection_run is None:
        return {}
    return {
        "collection_run_id": collection_run.id,
        "collection_finished_at": collection_run.finished_at.isoformat()
        if collection_run.finished_at
        else None,
    }


async def _paper_scan_for_collection(
    session: AsyncSession,
    symbol: str,
    collection_run_id: str,
) -> StrategyDecision | None:
    rows = await session.execute(
        select(StrategyDecision)
        .where(StrategyDecision.symbol == symbol)
        .order_by(StrategyDecision.created_at.desc())
        .limit(20)
    )
    for item in rows.scalars():
        risk_payload = item.risk_payload or {}
        if risk_payload.get("paper_scan_status") and _collection_run_id(item) == collection_run_id:
            return item
    return None


async def _confirmation_for_symbol(
    session: AsyncSession,
    symbol: str,
    direction: str,
    current_risk: dict[str, Any] | None = None,
    required: int = 2,
) -> dict[str, Any]:
    rows = await session.execute(
        select(StrategyDecision)
        .where(StrategyDecision.symbol == symbol)
        .order_by(StrategyDecision.created_at.desc())
        .limit(required * 4)
    )
    decisions = rows.scalars().all()
    streak = 0
    plan_checks: list[dict[str, Any]] = []
    baseline_risk = current_risk
    seen_collection_run_ids: set[str] = set()
    for item in decisions:
        risk_payload = item.risk_payload or {}
        gate = risk_payload.get("execution_gate") or {}
        collection_run_id = _collection_run_id(item)
        if not collection_run_id:
            continue
        if collection_run_id:
            if collection_run_id in seen_collection_run_ids:
                continue
            seen_collection_run_ids.add(collection_run_id)
        if item.decision != "open" or item.direction != direction or not gate.get("ready"):
            break
        if entry_plan_is_expired(risk_payload.get("entry_plan")):
            plan_checks.append({"decision_id": item.id, "compatible": False, "reasons": ["entry_plan_expired"]})
            break
        if baseline_risk is not None:
            compatibility = entry_plan_compatibility(baseline_risk, risk_payload)
            plan_checks.append({"decision_id": item.id, **compatibility})
            if not compatibility["compatible"]:
                break
        baseline_risk = risk_payload
        streak += 1
        if streak >= required:
            break
        continue
    return {
        "required": required,
        "streak": streak,
        "confirmed": streak >= required,
        "plan_compatible": streak >= required,
        "plan_checks": plan_checks,
        "label": "连续确认" if streak >= required else "等待连续确认",
    }


def _collection_run_id(decision: StrategyDecision) -> str | None:
    context = ((decision.risk_payload or {}).get("paper_scan_context") or {})
    raw_id = context.get("collection_run_id")
    return str(raw_id) if raw_id else None


async def _latest_decision(session: AsyncSession, symbol: str) -> StrategyDecision | None:
    rows = await session.execute(
        select(StrategyDecision)
        .where(StrategyDecision.symbol == symbol)
        .order_by(StrategyDecision.created_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _record_paper_scan_status(
    session: AsyncSession,
    decision: StrategyDecision | None,
    *,
    confirmation: dict[str, Any],
    status: str,
    scan_context: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    if decision is None:
        return
    risk_payload = dict(decision.risk_payload or {})
    risk_payload["confirmation"] = confirmation
    risk_payload["paper_scan_status"] = status
    if scan_context:
        risk_payload["paper_scan_context"] = scan_context
    decision.risk_payload = risk_payload
    if commit:
        await session.commit()


def _evaluation_payload(evaluation) -> dict[str, Any]:
    return {
        "symbol": evaluation.symbol,
        "direction": evaluation.direction,
        "score": evaluation.score,
        "decision": evaluation.decision,
        "price": evaluation.price,
        "risk": evaluation.risk_payload,
        "reason": evaluation.reason,
        "journal": _journal_from_reason(evaluation.reason),
    }


def _pnl(direction: str, entry: float, price: float) -> float:
    if direction == "long":
        return (price - entry) / entry
    return (entry - price) / entry


def _exit_reason(trade: PaperTrade, price: float) -> str | None:
    stop_reason = _stop_exit_reason(trade, price)
    if stop_reason:
        return stop_reason
    if _take_profit_touched(trade, price):
        return "take_profit"
    return None


def _stop_exit_reason(trade: PaperTrade, price: float) -> str | None:
    if trade.direction == "long":
        if price <= trade.stop_loss:
            return "trailing_stop" if trade.stop_loss > trade.entry_price else "stop_loss"
    else:
        if price >= trade.stop_loss:
            return "trailing_stop" if trade.stop_loss < trade.entry_price else "stop_loss"
    return None


def _take_profit_touched(trade: PaperTrade, price: float) -> bool:
    if trade.direction == "long":
        return price >= trade.take_profit
    if trade.direction == "short":
        return price <= trade.take_profit
    return False


async def _take_profit_runner_decision(session: AsyncSession, trade: PaperTrade, price: float) -> dict[str, Any]:
    if not _runner_asset_eligible(trade):
        return _runner_decision(False, blockers=["asset_not_runner_eligible"])
    if trade.entry_price <= 0 or price <= 0:
        return _runner_decision(False, blockers=["invalid_price"])
    if trade.direction == "long" and price <= trade.entry_price:
        return _runner_decision(False, blockers=["not_profitable"])
    if trade.direction == "short" and price >= trade.entry_price:
        return _runner_decision(False, blockers=["not_profitable"])

    latest = await _latest_decision(session, trade.symbol)
    opening = await session.get(StrategyDecision, trade.strategy_decision_id)
    if latest and latest.id != getattr(opening, "id", None) and latest.direction != trade.direction:
        return _runner_decision(
            False,
            blockers=["latest_direction_changed"],
            decision_id=latest.id,
            decision_created_at=latest.created_at.isoformat() if latest.created_at else None,
        )
    if latest and _fresh_runner_decision(latest):
        evidence = _runner_evidence_from_decision(latest, trade)
        return _runner_decision_from_evidence(evidence, latest)
    if opening is None:
        return _runner_decision(False, blockers=["missing_fresh_strategy_decision"])
    evidence = _runner_evidence_from_decision(opening, trade, allow_stale_opening=True)
    return _runner_decision_from_evidence(evidence, opening)


def _runner_decision_from_evidence(evidence: dict[str, Any], decision: StrategyDecision) -> dict[str, Any]:
    return _runner_decision(
        _runner_extension_allowed(evidence),
        score=evidence["score"],
        signals=evidence["signals"],
        blockers=evidence["blockers"],
        decision_id=decision.id,
        decision_created_at=decision.created_at.isoformat() if decision.created_at else None,
    )


def _runner_extension_allowed(evidence: dict[str, Any]) -> bool:
    signals = evidence.get("signals") or {}
    return (
        float(evidence.get("score") or 0.0) >= RUNNER_EVIDENCE_MIN_SCORE
        and not evidence.get("blockers")
        and (signals.get("dark_flow_target") or signals.get("trend_aligned"))
        and (signals.get("liquidity_context") or signals.get("orderflow_confirmed"))
    )


async def _latest_runner_decision(session: AsyncSession, trade: PaperTrade) -> StrategyDecision | None:
    latest = await _latest_decision(session, trade.symbol)
    if latest and _fresh_runner_decision(latest):
        return latest
    opening = await session.get(StrategyDecision, trade.strategy_decision_id)
    if opening and _fresh_runner_decision(opening):
        return opening
    return None


def _runner_evidence_from_decision(
    decision: StrategyDecision,
    trade: PaperTrade,
    *,
    allow_stale_opening: bool = False,
) -> dict[str, Any]:
    risk = decision.risk_payload or {}
    reason = decision.reason or {}
    rules = set(reason.get("rules") or [])
    gate = risk.get("execution_gate") or {}
    target_source = str(risk.get("target_source") or "")
    min_score = float(risk.get("min_score") or template_for_tier(trade.asset_tier).min_score)
    fresh = _fresh_runner_decision(decision)
    opening_age_allowed = _opening_evidence_age_allowed(decision, trade)
    opening_fallback = allow_stale_opening and not fresh and decision.id == trade.strategy_decision_id and opening_age_allowed
    signals = {
        "same_direction": decision.direction == trade.direction,
        "fresh": fresh,
        "opening_evidence_fallback": opening_fallback,
        "score_above_minimum": float(decision.score or 0.0) >= min_score,
        "execution_ready": bool(gate.get("ready")) or decision.decision == "open",
        "dark_flow_target": bool(target_source and target_source != "risk_reward_template"),
        "trend_aligned": {"long_term_direction", "mid_term_aligned", "short_term_aligned"}.issubset(rules),
        "liquidity_context": "liquidity_context_present" in rules,
        "orderflow_confirmed": "orderflow_present" in rules,
        "exhaustion_filter_present": "exhaustion_present" in rules,
    }
    blockers: list[str] = []
    if not signals["same_direction"]:
        blockers.append("latest_direction_changed")
    if not signals["fresh"] and not opening_fallback:
        blockers.append("latest_decision_stale")
    if allow_stale_opening and decision.id == trade.strategy_decision_id and not opening_age_allowed:
        blockers.append("opening_evidence_too_old")
    score = (
        (1.5 if signals["same_direction"] else 0.0)
        + (1.0 if signals["fresh"] else 0.6 if opening_fallback else 0.0)
        + (1.0 if signals["score_above_minimum"] else 0.0)
        + (1.25 if signals["execution_ready"] else 0.0)
        + (1.25 if signals["dark_flow_target"] else 0.0)
        + (1.25 if signals["trend_aligned"] else 0.0)
        + (0.9 if signals["liquidity_context"] else 0.0)
        + (0.9 if signals["orderflow_confirmed"] else 0.0)
        + (0.4 if signals["exhaustion_filter_present"] else 0.0)
    )
    return {"score": round(score, 3), "signals": signals, "blockers": blockers}


def _opening_evidence_age_allowed(decision: StrategyDecision, trade: PaperTrade) -> bool:
    reference = trade.opened_at or decision.created_at
    if reference is None:
        return False
    age_minutes = (datetime.now(timezone.utc) - _aware(reference)).total_seconds() / 60
    return age_minutes <= RUNNER_OPENING_EVIDENCE_MAX_AGE_MINUTES


def _fresh_runner_decision(decision: StrategyDecision) -> bool:
    if not decision.created_at:
        return False
    age_minutes = (datetime.now(timezone.utc) - _aware(decision.created_at)).total_seconds() / 60
    return age_minutes <= RUNNER_EVIDENCE_MAX_AGE_MINUTES


def _runner_decision(
    extend: bool,
    *,
    score: float = 0.0,
    signals: dict[str, bool] | None = None,
    blockers: list[str] | None = None,
    decision_id: str | None = None,
    decision_created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "extend": extend,
        "score": round(score, 3),
        "min_score": RUNNER_EVIDENCE_MIN_SCORE,
        "signals": signals or {},
        "blockers": blockers or [],
        "decision_id": decision_id,
        "decision_created_at": decision_created_at,
    }


def _extend_take_profit_runner(trade: PaperTrade, price: float) -> None:
    profit_distance = abs(price - trade.entry_price)
    extension = max(profit_distance * RUNNER_EXTENSION_FRACTION, trade.entry_price * RUNNER_MIN_EXTENSION_PCT)
    if trade.direction == "long":
        locked_stop = trade.entry_price + profit_distance * RUNNER_LOCK_FRACTION
        trade.stop_loss = max(trade.stop_loss, locked_stop)
        trade.take_profit = max(trade.take_profit, price + extension)
        return
    locked_stop = trade.entry_price - profit_distance * RUNNER_LOCK_FRACTION
    trade.stop_loss = min(trade.stop_loss, locked_stop)
    trade.take_profit = min(trade.take_profit, price - extension)


def _runner_asset_eligible(trade: PaperTrade) -> bool:
    tier = str(getattr(trade, "asset_tier", "") or "")
    if tier in RUNNER_TIERS:
        return True
    return str(getattr(trade, "symbol", "") or "").upper() in RUNNER_SYMBOLS


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _journal(decision: StrategyDecision) -> dict[str, Any]:
    return _journal_from_reason(decision.reason)


def _journal_from_reason(reason: dict[str, Any]) -> dict[str, Any]:
    explanations = reason.get("explanation") or []
    warnings = reason.get("warnings") or []
    rules = reason.get("rules") or []
    return {
        "开仓理由": explanations,
        "风险提示": warnings,
        "触发规则": rules,
        "结论": _journal_conclusion(explanations, warnings),
    }


def _journal_conclusion(explanations: list[str], warnings: list[str]) -> str:
    if warnings:
        return "允许继续观察，缺失项补齐前不应当过度信任。"
    if explanations:
        return "信号结构较完整，可进入纸上交易验证。"
    return "信号不足，保持观察。"


def _format_open_message(trade: PaperTrade, decision: StrategyDecision) -> str:
    direction = "做多" if trade.direction == "long" else "做空"
    warnings = decision.reason.get("warnings") or []
    warning_text = "\n".join(f"- {item}" for item in warnings[:4]) or "- 暂无"
    return (
        f"HFD 纸上交易开仓\n"
        f"标的：{trade.symbol}\n"
        f"方向：{direction}\n"
        f"入场：{trade.entry_price:.6g}\n"
        f"止损：{trade.stop_loss:.6g}\n"
        f"止盈：{trade.take_profit:.6g}\n"
        f"评分：{decision.score:.1f}\n"
        f"风险提示：\n{warning_text}"
    )


def _format_candidate_message(payload: dict[str, Any]) -> str:
    risk = payload.get("risk") or {}
    direction = "做多" if payload.get("direction") == "long" else "做空"
    gate = risk.get("execution_gate") or {}
    confirmation = risk.get("confirmation") or {}
    cost_zone = risk.get("entry_zone") or {}
    execution_zone = risk.get("execution_zone") or {}
    warnings = ((payload.get("reason") or {}).get("warnings") or [])[:4]
    warning_text = "\n".join(f"- {item}" for item in warnings) or "- 暂无"
    return (
        "HFD 纸上扫描候选\n"
        f"标的：{payload.get('symbol')}\n"
        f"方向：{direction}\n"
        f"当前价：{_fmt_price(payload.get('price'))}\n"
        f"成本观察区：{_fmt_price(cost_zone.get('lower'))} - {_fmt_price(cost_zone.get('upper'))}\n"
        f"可执行区：{_fmt_zone(execution_zone)}\n"
        f"计划入场：{_fmt_price(risk.get('entry_price'))}\n"
        f"暗流止损：{_fmt_price(risk.get('stop_loss'))}（{risk.get('stop_source', '--')}）\n"
        f"暗流止盈：{_fmt_price(risk.get('take_profit'))}（{risk.get('target_source', '--')}）\n"
        f"评分：{payload.get('score', 0):.1f}\n"
        f"执行门槛：{'通过' if gate.get('ready') else '未通过'}\n"
        f"连续确认：{confirmation.get('streak', 0)}/{confirmation.get('required', 2)}\n"
        f"风险提示：\n{warning_text}"
    )


def _fmt_price(value: Any) -> str:
    return f"{value:.6g}" if isinstance(value, (int, float)) else "--"


def _fmt_zone(zone: dict[str, Any]) -> str:
    if not zone.get("valid") or not isinstance(zone.get("lower"), (int, float)) or not isinstance(zone.get("upper"), (int, float)):
        return "--"
    return f"{zone['lower']:.6g} - {zone['upper']:.6g}"


def _format_close_message(trade: PaperTrade, pnl_pct: float, exit_reason: str) -> str:
    return (
        f"HFD 纸上交易出场\n"
        f"标的：{trade.symbol}\n"
        f"原因：{exit_reason}\n"
        f"出场价：{trade.exit_price:.6g}\n"
        f"收益率：{pnl_pct:.2%}\n"
        f"R 倍数：{trade.r_multiple:.2f}"
    )


async def _notify_safe(text: str) -> None:
    try:
        client = TelegramClient()
        if client.configured and client.chat_id:
            await client.send_message(text)
    except Exception:
        return
