"""阶段准备的身份和恢复边界；真实服务验收另由私有运行回执证明。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import phase_experiment as phase


class PhasePreparationTests(unittest.TestCase):
    def test_same_commit_does_not_allow_different_image_tree_or_build(self):
        planned={"commit":"a"*40,"tree":"b"*40,"image_id":"sha256:"+"c"*64,
            "build_operation_id":"build","repository_url":"registered"}
        actual={"sut_commit":planned["commit"],"sut_tree":planned["tree"],"sut_image_id":planned["image_id"]}
        manifest={"build_operation_id":"build","product_repository_url":"registered"}
        phase.match_planned_candidate(planned,actual,manifest)
        for field in actual:
            changed={**actual,field:"other"}
            with self.assertRaisesRegex(phase.SeedError,"ACTUAL_CANDIDATE_MISMATCH"):
                phase.match_planned_candidate(planned,changed,manifest)
        for field in manifest:
            with self.assertRaisesRegex(phase.SeedError,"ACTUAL_CANDIDATE_MISMATCH"):
                phase.match_planned_candidate(planned,actual,{**manifest,field:"other"})

    def test_initial_recovery_reads_again_and_preserves_first_qualification(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=phase.PhaseSource.__new__(phase.PhaseSource)
            source.directory=Path(temporary);source.state={};source.native={"run_id":"test"}
            source.specs={"late_usage":{},"post_claim_usage":{}};source.deployment_binding={"source":"fixed"}
            source.prepare=Mock();source.observe_initial_state=Mock(return_value={"status":"READY"})
            source.prepare_initial()
            first=(source.directory/"initial-qualification.json").read_bytes()
            ids={call.kwargs["observation_id"] for call in source.observe_initial_state.call_args_list}
            # 后一次读回不满足前提，不能拿旧 READY 继续声称“初态就绪”。
            source.observe_initial_state.reset_mock()
            source.observe_initial_state.return_value={"status":"NOT_READY"}
            with self.assertRaisesRegex(phase.SeedError,"INITIAL_STATE_NOT_READY"):
                source.prepare_initial()
            self.assertEqual((source.directory/"initial-qualification.json").read_bytes(),first)
            self.assertEqual(len(list((source.directory/"initial-observations").glob("*.json"))),2)
            self.assertTrue(ids.isdisjoint({call.kwargs["observation_id"] for call in source.observe_initial_state.call_args_list}))
            self.assertTrue(all(row["status"]=="NOT_READY" for row in json.loads(
                (source.directory/"latest-initial-qualification.json").read_text())["observations"].values()))

    def test_advanced_experiment_cannot_be_relabelled_as_initial(self):
        for field in ("usage_dispatches","usage_completions"):
            source=phase.PhaseSource.__new__(phase.PhaseSource)
            source.state={field:{"late_usage":{"status":"SENT"}}};source.prepare=Mock()
            with self.assertRaisesRegex(phase.SeedError,"INITIAL_PHASE_ALREADY_ADVANCED"):
                source.prepare_initial()
            source.prepare.assert_not_called()

    def test_output_must_stay_in_exact_private_experiment_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();(root/"artifacts/phase-experiments").mkdir(parents=True)
            with patch.object(phase,"ROOT",root):
                valid=root/"artifacts/phase-experiments/experiment-a"
                self.assertEqual(phase.experiment_directory(valid),valid)
                self.assertEqual(valid.stat().st_mode & 0o777,0o700)
                for path in (root/"docs/new",root/"artifacts/phase-experiments/experiment-a/nested"):
                    with self.assertRaisesRegex(phase.SeedError,"PRIVATE_PHASE_EXPERIMENT_DIRECTORY_REQUIRED"):
                        phase.experiment_directory(path)
                link=root/"artifacts/phase-experiments/experiment-b";link.symlink_to(valid)
                with self.assertRaisesRegex(phase.SeedError,"PRIVATE_PHASE_EXPERIMENT_DIRECTORY_REQUIRED"):
                    phase.experiment_directory(link)


if __name__=="__main__":unittest.main()
