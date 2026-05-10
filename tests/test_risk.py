from datetime import datetime, timezone

from app.infrastructure.raw_store import LocalRawPayloadStore
from app.services.risk import build_stop_plan, build_target_plan, build_trade_levels, template_for_tier


def test_risk_templates_are_tiered() -> None:
    assert template_for_tier("core").min_score < template_for_tier("high_volatility").min_score
    assert template_for_tier("core").risk_fraction > template_for_tier("high_volatility").risk_fraction


def test_trade_levels_for_long_and_short() -> None:
    long_levels = build_trade_levels("long", 100.0, "core")
    short_levels = build_trade_levels("short", 100.0, "core")

    assert long_levels["stop_loss"] < 100.0 < long_levels["take_profit"]
    assert short_levels["take_profit"] < 100.0 < short_levels["stop_loss"]


def test_target_plan_prefers_valid_dark_flow_target() -> None:
    plan = build_target_plan(
        direction="short",
        entry_price=100.0,
        stop_loss=101.0,
        fallback_take_profit=98.0,
        asset_tier="core",
        states=[],
        snapshots={
            "liq_heatmap": type(
                "Snapshot",
                (),
                {
                    "raw_payload": {
                        "heatmap_data": [
                            {"price": 99.4, "intensity": 1.0},
                            {"price": 98.5, "intensity": 0.8},
                        ]
                    }
                },
            )()
        },
    )

    assert plan["primary_target"] == 98.5
    assert plan["source"] == "liq_heatmap.heatmap_data"


def test_target_plan_falls_back_when_dark_flow_target_is_too_close() -> None:
    plan = build_target_plan(
        direction="long",
        entry_price=100.0,
        stop_loss=99.0,
        fallback_take_profit=102.0,
        asset_tier="core",
        states=[],
        snapshots={
            "liq_heatmap": type(
                "Snapshot",
                (),
                {"raw_payload": {"heatmap_data": [{"price": 100.5, "intensity": 1.0}]}},
            )()
        },
    )

    assert plan["primary_target"] == 102.0
    assert plan["source"] == "risk_reward_template"


def test_target_plan_uses_first_reachable_target_before_far_liquidity() -> None:
    plan = build_target_plan(
        direction="short",
        entry_price=100.0,
        stop_loss=101.0,
        fallback_take_profit=98.0,
        asset_tier="core",
        states=[],
        snapshots={
            "liq_heatmap": type(
                "Snapshot",
                (),
                {
                    "raw_payload": {
                        "heatmap_data": [
                            {"price": 90.0, "intensity": 1.0},
                            {"price": 97.5, "intensity": 0.4},
                        ]
                    }
                },
            )()
        },
    )

    assert plan["primary_target"] == 97.5


def test_stop_plan_prefers_dark_flow_invalidation_level_for_long() -> None:
    plan = build_stop_plan(
        direction="long",
        entry_price=100.0,
        fallback_stop_loss=99.0,
        asset_tier="core",
        states=[],
        snapshots={
            "liq_heatmap": type(
                "Snapshot",
                (),
                {"raw_payload": {"heatmap_data": [{"price": 98.8, "intensity": 1.0}]}},
            )()
        },
    )

    assert round(plan["stop_loss"], 4) == 98.6518
    assert plan["source"] == "liq_heatmap.heatmap_data"


def test_stop_plan_falls_back_when_dark_flow_stop_is_too_close() -> None:
    plan = build_stop_plan(
        direction="long",
        entry_price=100.0,
        fallback_stop_loss=99.0,
        asset_tier="core",
        states=[],
        snapshots={
            "liq_heatmap": type(
                "Snapshot",
                (),
                {"raw_payload": {"heatmap_data": [{"price": 99.7, "intensity": 1.0}]}},
            )()
        },
    )

    assert plan["stop_loss"] == 99.0
    assert plan["source"] == "risk_template_stop"


def test_trade_levels_include_dark_flow_stop_metadata() -> None:
    levels = build_trade_levels(
        "long",
        100.0,
        "core",
        snapshots={
            "liq_heatmap": type(
                "Snapshot",
                (),
                {
                    "raw_payload": {
                        "heatmap_data": [
                            {"price": 98.8, "intensity": 1.0},
                            {"price": 103.0, "intensity": 0.9},
                        ]
                    }
                },
            )()
        },
    )

    assert levels["stop_source"] == "liq_heatmap.heatmap_data"
    assert levels["target_source"] == "liq_heatmap.heatmap_data"
    assert levels["stop_candidates"]
    assert levels["target_candidates"]


def test_trade_levels_tolerate_non_numeric_signal_intensity() -> None:
    levels = build_trade_levels(
        "long",
        100.0,
        "core",
        snapshots={
            "liq_heatmap": type(
                "Snapshot",
                (),
                {
                    "raw_payload": {
                        "heatmap_data": [
                            {"price": 98.8, "intensity": "hot"},
                            {"price": 103.0, "intensity": None},
                        ]
                    }
                },
            )()
        },
    )

    assert levels["stop_source"] == "liq_heatmap.heatmap_data"
    assert levels["target_source"] == "liq_heatmap.heatmap_data"


def test_trade_levels_read_externalized_snapshot_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RAW_PAYLOAD_DIR", str(tmp_path))
    store = LocalRawPayloadStore(tmp_path)
    ref = store.write_json(
        payload={"heatmap_data": [{"price": 98.8, "intensity": 1.0}, {"price": 103.0, "intensity": 1.0}]},
        symbol="BTCUSDT",
        timeframe="short",
        indicator="liq_heatmap",
        snapshot_id="snapshot-1",
        collected_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    snapshot = type(
        "Snapshot",
        (),
        {
            "raw_payload": {},
            "raw_payload_uri": ref.uri,
            "raw_payload_sha256": ref.sha256,
            "raw_payload_compression": ref.compression,
        },
    )()

    levels = build_trade_levels("long", 100.0, "core", snapshots={"liq_heatmap": snapshot})

    assert levels["stop_source"] == "liq_heatmap.heatmap_data"
    assert levels["target_source"] == "liq_heatmap.heatmap_data"
