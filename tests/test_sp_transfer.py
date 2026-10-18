"""USB transfer tests use a simulated CLI exclusively; no device is written."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from cratebuilder.kit_export import export_kit
from cratebuilder.model import Assignment, Sample, Session
from cratebuilder import sp_transfer as transfer


def status_text(project=1, name="MY PROJECT", protected=()):
    lines = ["port:          COM4", f"project:       {project} ({name})", "project tempo: 120.00  (tempo select: bank)"]
    lines += [f"bank {bank}:        tempo 120.00  volume 127" + ("  protected" if bank in protected else "") for bank in transfer.BANKS]
    return "\n".join(lines + ["free space:    12.5 GB", ""])


def snapshot(occupied=False):
    pads = transfer.parse_pads("B13       4800 frames mono    level 100  bpm 120.00  original kitchen hit\n") if occupied else {}
    return {**transfer.parse_status(status_text()), "device_serial": "USB-123", "occupied_pads": pads, "occupancy_known": True}


class FakeCLI:
    def __init__(self, fail_pad=None, bad_backup=False, cancel=None):
        self.commands = []
        self.fail_pad, self.bad_backup, self.cancel = fail_pad, bad_backup, cancel

    def __call__(self, cli, arguments, port=None, timeout=90):
        args = [str(value) for value in arguments]
        self.commands.append((args, port))
        if args[0] == "status":
            return status_text()
        if args[0] == "get":
            path = Path(args[2])
            if self.bad_backup:
                return "0 bytes -> missing"
            path.write_bytes(b"RFWVsample-backup" if path.suffix == ".SMP" else b"padconf-data")
            return f"{path.stat().st_size} bytes -> {path}"
        if args[0] == "padconf":
            return "project name:  MY PROJECT\n"
        if args[0] == "dump-state":
            folder = Path(args[1])
            folder.mkdir()
            for name in ("status.bin", "settings.bin", "pad-B13.bin"):
                (folder / name).write_bytes(b"live-parameters")
            return "saved"
        if args[0] == "smp-info":
            return "digest ok: true\n1 channels, 4800 frames, 0.100 s\n"
        if args[0] == "import":
            if args[1] == self.fail_pad:
                raise transfer.TransferError("Simulated USB disconnect during import")
            if self.cancel:
                self.cancel.set()
            return f"{args[1]}: imported 4800 frames as {args[-1]!r} into project {args[args.index('--project') + 1]}\n"
        raise AssertionError(f"Unexpected CLI command: {args}")


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        path = self.root / "tiny source.wav"
        sf.write(path, np.sin(np.arange(882) * .1) * .4, 44100, subtype="PCM_16")
        stat = path.stat()
        features = dict(centroid=1000, decay=.02, crest=10, flatness=.03, clipped=0, leading=0,
                        active_duration=.02, waveform=[.4], available=True)
        self.sample = Sample("source-id", str(path), path.name, "foley", 1, "Folder rule", .02,
                             44100, 1, hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns, features)
        self.session = Session(str(self.root), [self.sample], kit_size=16, target="sp404")
        self.session.pads[0] = Assignment(self.sample.id)
        self.session.pads[4] = Assignment(self.sample.id)
        self.export, _ = export_kit(self.session, 0, self.root / "exports")
        self.cli = self.root / "sp404.exe"
        self.cli.write_bytes(b"test fixture; never executed")

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, occupied=False):
        return transfer.make_transfer_plan(self.session, 0, "B", snapshot(occupied))

    def run_send(self, plan, fake, **kwargs):
        with patch.object(transfer, "probe", return_value=snapshot(bool(plan.overwrites))), patch.object(transfer, "_run", side_effect=fake):
            return transfer.send(plan, self.cli, self.export, reviewed=True, **kwargs)

    def test_parses_real_upstream_status_and_pad_formats(self):
        state = transfer.parse_status(status_text(name="A name (with parentheses)", protected="J"))
        self.assertEqual((state["project"], state["project_name"]), (1, "A name (with parentheses)"))
        self.assertTrue(state["banks"]["J"]["protected"])
        self.assertFalse(state["banks"]["A"]["protected"])
        pads = transfer.parse_pads("A1         9600 frames stereo  level 127  bpm 120.00  kick name\nJ16        4800 frames         level  70  bpm  90.00  unnamed channels\n")
        self.assertEqual(pads["J16"]["channels"], "unknown")
        self.assertEqual(pads["A1"]["frames"], 9600)
        self.assertEqual(transfer.parse_pads(""), {})

    def test_unknown_or_duplicate_output_fails_closed(self):
        for text in ("No occupied pads", '{"pads":[]}', "K1 23 frames mono level 1 bpm 120 bad"):
            with self.subTest(text=text), self.assertRaises(transfer.TransferError):
                transfer.parse_pads(text)
        with self.assertRaises(transfer.TransferError):
            transfer.parse_status("port: COM4\nproject: 1 (X)\n")
        duplicate = "A1 4800 frames mono level 100 bpm 120 hit\n" * 2
        with self.assertRaises(transfer.TransferError):
            transfer.parse_pads(duplicate)

    def test_probe_is_read_only_and_pins_the_usb_port(self):
        calls = []
        def run(cli, arguments, port=None, timeout=90):
            calls.append((arguments, port))
            return {"ports": "COM4\tSP-404MKII\tUSB-123\n", "status": status_text(), "pads": ""}[arguments[0]]
        with patch.object(transfer, "_run", side_effect=run):
            result = transfer.probe(self.cli)
        self.assertEqual([args[0] for args, _ in calls], ["ports", "status", "pads", "status"])
        self.assertTrue(all(port == "COM4" for _, port in calls[1:]))
        self.assertTrue(result["occupancy_known"])
        self.assertEqual(result["device_serial"], "USB-123")

    def test_probe_rejects_project_switch_and_ambiguous_devices(self):
        with patch.object(transfer, "_run", side_effect=["COM4\tSP\t123\nCOM5\tSP\t456\n"]):
            with self.assertRaisesRegex(transfer.TransferError, "More than one"):
                transfer.probe(self.cli)
        with patch.object(transfer, "_run", side_effect=["COM4\tSP\tUSB-123\n", status_text(), "", status_text(2)]):
            with self.assertRaisesRegex(transfer.TransferError, "changed while"):
                transfer.probe(self.cli)

    def test_plan_is_immutable_and_maps_bottom_core_to_actual_sp_pads(self):
        plan = self.plan(True)
        self.assertEqual([entry.target_pad for entry in plan.entries], ["B13", "B9"])
        self.assertEqual(plan.overwrites, ("B13",))
        self.assertEqual(plan.entries[0].previous_name, "original kitchen hit")
        with self.assertRaises(FrozenInstanceError):
            plan.bank = "A"
        with self.assertRaises(FrozenInstanceError):
            plan.entries[0].target_pad = "A1"

    def test_unknown_occupancy_and_protected_bank_cannot_be_planned(self):
        for state in ({**snapshot(), "occupancy_known": False},
                      {**snapshot(), "banks": {"B": {"protected": True}}}):
            with self.assertRaises(transfer.TransferError):
                transfer.make_transfer_plan(self.session, 0, "B", state)

    def test_review_and_overwrite_consent_are_required_before_any_usb_command(self):
        with patch.object(transfer, "probe") as probe, patch.object(transfer, "_run") as run:
            with self.assertRaisesRegex(transfer.TransferError, "Review"):
                transfer.send(self.plan(), self.cli, self.export)
            with self.assertRaisesRegex(transfer.TransferError, "Explicitly allow"):
                transfer.send(self.plan(True), self.cli, self.export, reviewed=True)
            with self.assertRaisesRegex(transfer.TransferError, "backup folder"):
                transfer.send(self.plan(True), self.cli, self.export, reviewed=True, allow_overwrite=True)
            probe.assert_not_called()
            run.assert_not_called()

    def test_changed_export_is_rejected_before_usb_access(self):
        next(self.export.glob("*.wav")).write_bytes(b"tampered audio")
        with patch.object(transfer, "probe") as probe:
            with self.assertRaisesRegex(transfer.TransferError, "missing, changed"):
                transfer.send(self.plan(), self.cli, self.export, reviewed=True)
            probe.assert_not_called()

    def test_changed_project_or_occupancy_prevents_first_write(self):
        for state in ({**snapshot(), "project": 2}, snapshot(True)):
            with patch.object(transfer, "probe", return_value=state), patch.object(transfer, "_run") as run:
                with self.assertRaises(transfer.TransferError):
                    transfer.send(self.plan(), self.cli, self.export, reviewed=True)
                run.assert_not_called()

    def test_project_switch_during_backup_prevents_any_import(self):
        fake = FakeCLI()
        with patch.object(transfer, "probe", side_effect=[snapshot(), {**snapshot(), "project": 2}]), patch.object(transfer, "_run", side_effect=fake):
            with self.assertRaises(transfer.TransferError) as caught:
                transfer.send(self.plan(), self.cli, self.export, reviewed=True)
        self.assertEqual(caught.exception.report["completed_pads"], [])
        self.assertFalse(any(args[0] == "import" for args, _ in fake.commands))

    def test_failed_backup_prevents_all_imports(self):
        fake = FakeCLI(bad_backup=True)
        with self.assertRaisesRegex(transfer.TransferError, "backup was not saved") as caught:
            self.run_send(self.plan(True), fake, allow_overwrite=True, backup_dir=self.root / "backups")
        self.assertEqual(caught.exception.report["completed_pads"], [])
        self.assertFalse(any(args[0] == "import" for args, _ in fake.commands))

    def test_occupied_pad_backup_finishes_before_first_import_and_never_restores_project(self):
        fake = FakeCLI()
        report = self.run_send(self.plan(True), fake, allow_overwrite=True, backup_dir=self.root / "backups")
        commands = [args[0] for args, _ in fake.commands]
        self.assertLess(commands.index("smp-info"), commands.index("import"))
        self.assertNotIn("restore-project", commands)
        self.assertNotIn("init-project", commands)
        self.assertNotIn("select-project", commands)
        backup = Path(report["backup_dir"])
        manifest = json.loads((backup / "BACKUP_COMPLETE.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertTrue((backup / "BANK2-13.SMP").is_file())
        self.assertTrue((backup / "live-state" / "pad-B13.bin").is_file())
        self.assertEqual(report["completed_pads"], ["B13", "B9"])
        self.assertEqual(report["status"], "complete")
        imports = [args for args, _ in fake.commands if args[0] == "import"]
        self.assertTrue(all(args[args.index("--project") + 1] == "1" for args in imports))

    def test_partial_failure_preserves_completed_pads_and_reports_uncertain_failed_pad(self):
        fake = FakeCLI(fail_pad="B9")
        with self.assertRaises(transfer.TransferError) as caught:
            self.run_send(self.plan(), fake)
        report = caught.exception.report
        self.assertEqual(report["completed_pads"], ["B13"])
        self.assertEqual(report["failed_pad"], "B9")
        self.assertTrue(report["failed_pad_may_be_modified"])
        saved = json.loads(Path(report["journal"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["completed_pads"], ["B13"])
        self.assertEqual(saved["status"], "failed")

    def test_cancel_stops_between_pads_without_bogus_rollback(self):
        cancel = threading.Event()
        fake = FakeCLI(cancel=cancel)
        with self.assertRaises(transfer.TransferError) as caught:
            self.run_send(self.plan(), fake, cancel=cancel)
        self.assertEqual(caught.exception.report["status"], "cancelled")
        self.assertEqual(caught.exception.report["completed_pads"], ["B13"])
        self.assertFalse(caught.exception.report["failed_pad_may_be_modified"])
        self.assertEqual(sum(args[0] == "import" for args, _ in fake.commands), 1)

    def test_subprocess_uses_argument_lists_and_hides_windows_console(self):
        completed = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        with patch.object(transfer.subprocess, "run", return_value=completed) as run:
            transfer._run(self.cli, ["get", "remote path", self.root / "local path.bin"], "COM4")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], [str(self.cli), "--port", "COM4", "get"])
        self.assertFalse(kwargs["shell"])
        self.assertIn("local path.bin", args[0][-1])

    def test_explicit_missing_cli_does_not_silently_select_a_different_executable(self):
        with patch.object(transfer.subprocess, "run") as run:
            self.assertIsNone(transfer.discover_cli(self.root / "missing.exe"))
            self.assertEqual(transfer.discover_cli(self.cli), self.cli.resolve())
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
