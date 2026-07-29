"""Contract tests for current-slot policy persistence and its CLI writer."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    DEFAULT_SLOT_POLICY,
    AutoSwitchSettings,
    SharedProfileSettings,
    SlotPolicy,
    load_shared_profile_settings,
    load_settings,
    load_slot_policies,
    set_setting,
    set_slot_policy,
    settings_path,
    unset_slot_policy,
)
from claude_swap.switcher import ClaudeAccountSwitcher


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    with (
        patch("os.geteuid", return_value=1000, create=True),
        patch.object(sys, "argv", ["claude-swap", "slot-policy", *argv]),
    ):
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _backup_root(temp_home: Path) -> Path:
    if sys.platform == "linux":
        return temp_home / ".local" / "share" / "claude-swap"
    return temp_home / ".claude-swap-backup"


def _write_roster(root: Path, accounts: dict[str, dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "sequence": [int(slot) for slot in accounts],
                "accounts": accounts,
            }
        )
    )


class TestSlotPolicySettings:
    def test_absent_configuration_keeps_shared_profile_mode_off(self, tmp_path: Path):
        assert load_shared_profile_settings(tmp_path) == SharedProfileSettings()
        assert load_shared_profile_settings(tmp_path).enabled is False
        assert load_slot_policies(tmp_path) == {}
        assert DEFAULT_SLOT_POLICY == SlotPolicy(
            five_hour_ceiling_pct=90.0,
            weekly_ceiling_pct=100.0,
            priority=0,
        )

    def test_valid_current_slot_policies_load_with_field_defaults(
        self, tmp_path: Path
    ):
        settings_path(tmp_path).write_text(
            json.dumps(
                {
                    "autoswitch": {
                        "sharedProfile": {"enabled": True},
                        "slotPolicies": {
                            "1": {"fiveHourCeilingPct": 50},
                            "2": {
                                "weeklyCeilingPct": 80,
                                "priority": -4,
                            },
                        },
                    }
                }
            )
        )

        assert load_shared_profile_settings(tmp_path).enabled is True
        assert load_slot_policies(tmp_path) == {
            1: SlotPolicy(50.0, 100.0, 0),
            2: SlotPolicy(90.0, 80.0, -4),
        }

    def test_shared_profile_gate_is_explicit_and_slot_writes_do_not_enable_it(
        self, tmp_path: Path
    ):
        set_slot_policy(tmp_path, 3, five_hour_ceiling_pct=50)
        assert load_shared_profile_settings(tmp_path).enabled is False

        assert (
            set_setting(
                tmp_path, "autoswitch.sharedProfile.enabled", "true"
            )
            is True
        )
        assert load_shared_profile_settings(tmp_path).enabled is True
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_generic_config_command_cannot_write_slot_policy_json(
        self, tmp_path: Path
    ):
        with pytest.raises(ConfigError, match="unknown setting"):
            set_setting(tmp_path, "autoswitch.slotPolicies", "{}")

    @pytest.mark.parametrize(
        "slot_policies",
        [
            {"01": {"fiveHourCeilingPct": 50}},
            {"0": {"fiveHourCeilingPct": 50}},
            {"100": {"fiveHourCeilingPct": 50}},
            {"x": {"fiveHourCeilingPct": 50}},
            {"1": []},
            {"1": {"fiveHourCeilingPct": True}},
            {"1": {"fiveHourCeilingPct": float("nan")}},
            {"1": {"fiveHourCeilingPct": 0}},
            {"1": {"weeklyCeilingPct": 101}},
            {"1": {"priority": 1.5}},
            {"1": {"priority": 1001}},
        ],
    )
    def test_malformed_hand_edits_fail_closed(
        self, tmp_path: Path, slot_policies: dict
    ):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"slotPolicies": slot_policies}})
        )

        with pytest.raises(ConfigError, match="slotPolicies"):
            load_slot_policies(tmp_path)

        # Classic autoswitch remains on its forgiving scalar contract. The
        # safety error is observed only by the explicitly enabled controller.
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_writer_preserves_unknown_siblings_and_policy_fields(
        self, tmp_path: Path
    ):
        settings_path(tmp_path).write_text(
            json.dumps(
                {
                    "schemaVersion": 7,
                    "future": {"keep": True},
                    "autoswitch": {
                        "threshold": 77,
                        "slotPolicies": {
                            "3": {
                                "fiveHourCeilingPct": 45,
                                "futurePolicyField": "keep",
                            }
                        },
                    },
                }
            )
        )

        policy = set_slot_policy(
            tmp_path,
            3,
            five_hour_ceiling_pct=50,
            weekly_ceiling_pct=90,
            priority=10,
        )

        assert policy == SlotPolicy(50.0, 90.0, 10)
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["schemaVersion"] == 7
        assert raw["future"] == {"keep": True}
        assert raw["autoswitch"]["threshold"] == 77
        assert (
            raw["autoswitch"]["slotPolicies"]["3"]["futurePolicyField"] == "keep"
        )

    def test_unset_removes_only_requested_policy(self, tmp_path: Path):
        set_slot_policy(tmp_path, 1, five_hour_ceiling_pct=90)
        set_slot_policy(tmp_path, 2, five_hour_ceiling_pct=50)

        assert unset_slot_policy(tmp_path, 1) is True
        assert load_slot_policies(tmp_path) == {2: SlotPolicy(50.0, 100.0, 0)}
        assert unset_slot_policy(tmp_path, 1) is False

    def test_duplicate_policy_key_fails_closed(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            '{"autoswitch":{"slotPolicies":{"1":{"fiveHourCeilingPct":50},'
            '"1":{"fiveHourCeilingPct":90}}}}'
        )

        with pytest.raises(ConfigError, match="duplicate JSON key"):
            load_slot_policies(tmp_path)

    def test_roster_swap_leaves_policies_on_slot_numbers(
        self,
        temp_home: Path,
        sample_sequence_data: dict,
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        set_slot_policy(switcher.backup_dir, 1, five_hour_ceiling_pct=50)
        set_slot_policy(switcher.backup_dir, 2, five_hour_ceiling_pct=90)

        switcher.swap_accounts("1", "2")

        assert switcher._get_sequence_data()["accounts"]["1"]["email"] == (
            "account2@example.com"
        )
        assert load_slot_policies(switcher.backup_dir) == {
            1: SlotPolicy(50.0, 100.0, 0),
            2: SlotPolicy(90.0, 100.0, 0),
        }


class TestSlotPolicyCli:
    def test_set_list_and_unset(self, temp_home: Path, capsys):
        code, out, _ = _run(
            [
                "set",
                "3",
                "--5h-ceiling",
                "50",
                "--weekly-ceiling",
                "90",
                "--priority",
                "10",
            ],
            capsys,
        )
        assert code == 0
        assert "slot 3" in out.lower()
        assert "5h 50%" in out
        assert "weekly 90%" in out

        code, out, _ = _run(["list"], capsys)
        assert code == 0
        assert "3" in out
        assert "50" in out
        assert "vacant" in out

        code, out, _ = _run(["unset", "3"], capsys)
        assert code == 0
        assert "slot 3" in out.lower()
        assert load_slot_policies(_backup_root(temp_home)) == {}

    def test_set_rejects_noncanonical_slot_and_bad_values(
        self, temp_home: Path, capsys
    ):
        code, _, err = _run(
            ["set", "01", "--5h-ceiling", "50"], capsys
        )
        assert code == 1
        assert "canonical" in err

        code, _, err = _run(
            ["set", "3", "--5h-ceiling", "0"], capsys
        )
        assert code == 1
        assert "between 1 and 100" in err

    def test_audit_shows_explicit_defaulted_and_vacant(
        self, temp_home: Path, capsys
    ):
        root = _backup_root(temp_home)
        _write_roster(
            root,
            {
                "1": {"email": "owner@example.com"},
                "2": {"email": "coworker@example.com"},
            },
        )
        set_slot_policy(root, 1, five_hour_ceiling_pct=90)
        set_slot_policy(root, 3, five_hour_ceiling_pct=50)

        code, out, _ = _run(["audit"], capsys)
        assert code == 0
        assert "owner@example.com" in out
        assert "explicit" in out
        assert "defaulted" in out
        assert "vacant" in out

    def test_list_does_not_migrate_or_rewrite_roster(
        self, temp_home: Path, capsys
    ):
        root = _backup_root(temp_home)
        _write_roster(root, {"1": {"email": "owner@example.com"}})
        before = (root / "sequence.json").read_bytes()

        code, _, _ = _run(["list"], capsys)

        assert code == 0
        assert (root / "sequence.json").read_bytes() == before

    def test_strict_audit_blocks_roster_policy_drift(
        self, temp_home: Path, capsys
    ):
        root = _backup_root(temp_home)
        _write_roster(
            root,
            {
                "1": {"email": "owner@example.com"},
                "2": {"email": "unexpected@example.com"},
            },
        )
        set_slot_policy(root, 1, five_hour_ceiling_pct=90)
        set_slot_policy(root, 3, five_hour_ceiling_pct=50)

        code, _, err = _run(["audit", "--strict"], capsys)
        assert code == 1
        assert "slot 2 is occupied but has no explicit policy" in err
        assert "slot 3 has an explicit policy but is vacant" in err

        set_slot_policy(root, 2, five_hour_ceiling_pct=50)
        unset_slot_policy(root, 3)
        code, out, err = _run(["audit", "--strict"], capsys)
        assert code == 0
        assert "strict audit passed" in out.lower()
        assert err == ""

    def test_main_help_mentions_dedicated_writer(self, temp_home: Path, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--help"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 0
        assert "slot-policy" in capsys.readouterr().out
