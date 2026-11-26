"""QA 发布身份、原件指纹与原始操作关联不能由调用方摘要替代。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import phase_qa
from client import SourceClientError


class PublishedQATests(unittest.TestCase):
    def test_publisher_profile_and_approval_are_not_interchangeable(self):
        obj={"profile":"PHASE_JSON_ENTRY","content_type":"text/plain"}
        source={"commit":"a"*40,"path":"oracles/phase_entry.py"}
        receipt={"publisher_id":"lab:qa-publisher","publication_id":"publication-1", "metadata":{
            **obj,"source":source,"approval_state":"DRAFT","authority_scope":"SYNTHETIC_LOCAL_CALIBRATION"}}
        phase_qa.verify_publication(receipt,obj,source)
        for key,value in (("profile","OTHER"),("approval_state","APPROVED"),("source",{"commit":"b"*40})):
            with self.subTest(key=key):
                bad={**receipt,"metadata":{**receipt['metadata'],key:value}}
                with self.assertRaisesRegex(SourceClientError,"QA_PUBLICATION_PROVENANCE_MISMATCH"):
                    phase_qa.verify_publication(bad,obj,source)
        with self.assertRaises(SourceClientError):
            phase_qa.verify_publication({**receipt,"publisher_id":"lab:business-publisher"},obj,source)

    def test_archive_digest_and_symlink_are_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();folder=root/'artifacts/phase-experiments/test';folder.mkdir(parents=True)
            path=folder/'observation.json';path.write_text('{}');path.chmod(0o600)
            digest=hashlib.sha256(b'{}').hexdigest()
            with patch.object(phase_qa,'ROOT',root):
                self.assertEqual(phase_qa.private_bytes(path,digest),b'{}')
                path.write_text('{"changed":true}')
                with self.assertRaisesRegex(SourceClientError,'QA_EVIDENCE_DIGEST_MISMATCH'):
                    phase_qa.private_bytes(path,digest)
                link=folder/'link.json';link.symlink_to(path)
                with self.assertRaisesRegex(SourceClientError,'QA_EVIDENCE_PATH_INVALID'):
                    phase_qa.private_bytes(link,digest)

    def test_source_acl_failure_prevents_materializing_executable(self):
        # 即便本地 Git 与清单一致，来源拒绝也必须中止；没有离线缓存兜底。
        objects=json.loads(phase_qa.REGISTRATION.read_text())['objects'];raw=b'trusted fixture'
        manifest={'contract_id':phase_qa.CONTRACT,'version':'phase-v1','publisher_principal':'lab:qa-publisher',
            'authority_scope':'SYNTHETIC_LOCAL_CALIBRATION','approval_state':'DRAFT',
            'files':[{'path':o['path'],'sha256':hashlib.sha256(raw).hexdigest()} for o in objects]}
        git=Mock();git.read.side_effect=lambda commit,path:json.dumps(manifest).encode() if path=='phase-publication.v1.json' else raw
        client=Mock();client.config={'principal_id':'lab:oracle-reader'}
        client.read.side_effect=SourceClientError('ACL_UNAVAILABLE',503)
        with tempfile.TemporaryDirectory() as temp,patch.object(phase_qa,'git_reader',return_value=git), \
                patch.object(phase_qa.SourceClient,'from_file',return_value=client):
            directory=Path(temp)
            with self.assertRaisesRegex(SourceClientError,'ACL_UNAVAILABLE'):
                phase_qa.fetch_bundle(Path(temp),'a'*40,Path(temp)/'reader',directory)
            self.assertEqual(list((directory/'context').iterdir()),[])

    def test_exit_zero_with_invalid_result_is_persisted_as_failed(self):
        container={'Image':'sha256:test','Config':{'User':'10001:10001'},'Mounts':[],
            'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'Binds':None,'CapDrop':['ALL'],
                'SecurityOpt':['no-new-privileges:true'],'Memory':268435456,'PidsLimit':32,'NanoCpus':1000000000}}
        calls=[]
        def command(args):
            calls.append(args)
            if args[1]=='create':return 'container-test'
            if args[1]=='inspect':
                return json.dumps([container if len([c for c in calls if c[1]=='inspect'])==1 else
                    {'State':{'ExitCode':0,'OOMKilled':False}}])
            return ''
        def completed(*args,**kwargs):
            kwargs['stdout'].write(b'{}');return Mock(returncode=0)
        with tempfile.TemporaryDirectory() as temp,patch.object(phase_qa,'command',side_effect=command), \
                patch.object(phase_qa.subprocess,'run',side_effect=completed):
            directory=Path(temp)
            with self.assertRaisesRegex(SourceClientError,'QA_RESULT_CONTRACT_INVALID'):
                phase_qa.run_checker(directory,'sha256:test',{})
            recorded=json.loads((directory/'execution.json').read_text())
            self.assertEqual(recorded['status'],'FAILED')
            self.assertEqual(recorded['exit_code'],0)
            self.assertEqual(calls[-1],['docker','rm','container-test'])


if __name__=='__main__':unittest.main()
