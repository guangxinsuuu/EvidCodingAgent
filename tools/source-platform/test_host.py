"""每实例发布目录不可漂移；新增 QA 来源不改变既有规则实例身份。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import host
from client import SourceClientError


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        (self.root/'config').mkdir();self.old=self.root/'config/old.json';self.new=self.root/'config/new.json'
        self.old.write_text('{"objects":[]}');self.new.write_text('{"objects":[{}]}')
        self.instance=self.root/'instance';self.instance.mkdir()
        self.patch=patch.multiple(host,ROOT=self.root,REGISTRATION=self.old);self.patch.start()

    def tearDown(self):self.patch.stop();self.temp.cleanup()

    def manifest(self,path,ref=None):
        value={'registration_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        if ref is not None:value['registration_ref']=ref
        (self.instance/'manifest.json').write_text(json.dumps(value))

    def test_legacy_manifest_keeps_original_catalog(self):
        self.manifest(self.old)
        self.assertEqual(host.instance_registration(self.instance),self.old)

    def test_new_instance_is_independent_of_legacy_catalog(self):
        self.manifest(self.new,'config/new.json');self.old.write_text('changed old catalog')
        self.assertEqual(host.instance_registration(self.instance),self.new)
        self.new.write_text('changed new catalog')
        with self.assertRaisesRegex(SourceClientError,'SOURCE_DEPLOYMENT_CONFIG_CHANGED'):
            host.instance_registration(self.instance)

    def test_symlink_and_outside_config_rejected(self):
        link=self.root/'config/link.json';link.symlink_to(self.new)
        for path in (link,self.root/'instance/manifest.json'):
            with self.subTest(path=path),self.assertRaises(SourceClientError):host.registration_path(path)

    def test_additional_version_cannot_expand_object_or_publisher_scope(self):
        obj={'namespace':'jira','object_id':'TASK-1','publisher':'requirements-publisher','repository':'EvidEnterpriseSources','version':'1'}
        self.old.write_text(json.dumps({'objects':[obj]}));self.manifest(self.old)
        self.new.write_text(json.dumps({'objects':[{**obj,'version':'2'}]}))
        objects,registration=host.publication_objects(self.instance,self.new)
        self.assertEqual(objects[0]['version'],'2');self.assertEqual(registration['ref'],'config/new.json')
        for edit in ({'object_id':'TASK-2'},{'publisher':'qa-publisher'}):
            self.new.write_text(json.dumps({'objects':[{**obj,**edit,'version':'2'}]}))
            with self.assertRaisesRegex(SourceClientError,'VERSION_REGISTRATION_SCOPE_EXPANSION'):
                host.publication_objects(self.instance,self.new)


if __name__=='__main__':unittest.main()
