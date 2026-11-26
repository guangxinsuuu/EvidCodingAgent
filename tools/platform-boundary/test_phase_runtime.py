"""阶段执行的拒绝与观察边界；真实恢复与无重复副作用由运行报告和 PG 测试补充。"""
from copy import deepcopy
import unittest
from unittest.mock import Mock,patch

import phase_observe as observation
import phase_runtime as runtime


class PhaseRuntimeTests(unittest.TestCase):
    def test_observer_cannot_write_and_http_failure_is_not_empty_fact(self):
        receipts=[];port=observation.ReadPort("http://127.0.0.1:1234","test",receipts,"source")
        with patch.object(observation,"request",return_value=(503,{},{})) as request:
            with self.assertRaisesRegex(runtime.SeedError,"WRITE_FORBIDDEN"):
                port.request("POST","/v1/commands")
            request.assert_not_called()
            with self.assertRaisesRegex(runtime.SeedError,"HTTP_UNAVAILABLE"):
                port.request("GET","/v1/context")
        self.assertEqual(receipts[0]["http_status"],503)

    def test_done_without_model_originals_is_not_reusable_observation(self):
        full={"terminal":True,"protocol_status":"VALID","job_state":"DONE","delivery_state":"COMPLETE","gaps":[],
            **{key:{} for key in ("context","result","accepted_event","decision","model_request","model_output")}}
        self.assertTrue(runtime.observation_complete(full))
        for key in ("model_request","model_output","context"):
            partial=deepcopy(full);partial[key]=None
            self.assertFalse(runtime.observation_complete(partial))
        self.assertFalse(runtime.observation_complete({**full,"gaps":["ORIGINAL_MESSAGE_READBACK_MISSING_OR_AMBIGUOUS"]}))
        self.assertTrue(runtime.observation_complete({"terminal":True,"job_state":"FAILED"}))

    def test_candidate_drift_blocks_callback_even_if_operation_might_be_replayed(self):
        execution=runtime.PhaseExecution.__new__(runtime.PhaseExecution)
        execution.args=object();execution.ledger=Mock();callback=Mock()
        with patch.object(runtime,"bind_candidate",side_effect=runtime.SeedError("CANDIDATE_CHANGED")):
            with self.assertRaisesRegex(runtime.SeedError,"CANDIDATE_CHANGED"):
                execution.step("x","WRITE","WRITE",{},callback)
        execution.ledger.execute.assert_not_called();callback.assert_not_called()

    def test_changed_initial_state_stops_before_product_submission(self):
        execution=runtime.PhaseExecution.__new__(runtime.PhaseExecution)
        execution.experiment="test";execution.basis={"candidate":{"sut_commit":"a"*40}}
        execution.step=Mock(return_value={"status":"NOT_READY"});execution.submit=Mock()
        with self.assertRaisesRegex(runtime.SeedError,"INITIAL_STATE_CHANGED"):
            execution.run_case({"label":"late_usage"})
        execution.submit.assert_not_called()


if __name__=="__main__":unittest.main()
