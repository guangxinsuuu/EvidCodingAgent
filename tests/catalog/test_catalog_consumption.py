"""平台侧消费验收：验证目录身份、只读查询与 CLI，不导入被测业务。"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.registry.catalog_reader import (
    CATALOG_COMMIT, GAP_PATH, load_catalog, local_plan_scope,
)
from tools.registry.catalog_cli import main
from tools.registry.reference_resolver import MaterialError

ROOT = Path(__file__).resolve().parents[2]


class CatalogConsumptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scope = local_plan_scope(ROOT, CATALOG_COMMIT)
        cls.reader = load_catalog(ROOT, CATALOG_COMMIT, cls.scope)

    def test_real_catalog_retains_draft_and_original_counts(self):
        self.assertEqual(self.reader.validation['status'], 'VALID_DRAFT')
        self.assertEqual(self.reader.validation['counts']['pinned_source_files'], 66)
        self.assertEqual(self.reader.validation['counts']['historical_observed_stages'], 13)
        self.assertEqual(len(self.reader.assets('scenario')), 12)
        self.assertEqual(len(self.reader.assets('stage')), 14)
        self.assertFalse(self.reader.validation['dispatch_authorized'])
        self.assertEqual(self.reader.validation['current_candidate_result'], 'NOT_RUN')

    def test_container_revision_is_not_material_or_product_revision(self):
        result = self.reader.describe_asset('lago_api', kind='source')
        self.assertEqual(result['catalog_commit'], CATALOG_COMMIT)
        self.assertEqual(result['catalog_basis_commit'], 'cb3881c0a9bfd67fec4ac05f31457b3d95e587b3')
        self.assertNotIn('product_candidate_ref', result)

    def test_lookup_returns_copies_not_mutable_registry_state(self):
        asset = self.reader.get_asset('scenario', 'terminated_subscription')
        asset['dispatch_enabled'] = True
        asset['stages'].clear()
        self.assertFalse(self.reader.get_asset('scenario', 'terminated_subscription')['dispatch_enabled'])
        self.assertEqual(len(self.reader.get_asset('scenario', 'terminated_subscription')['stages']), 1)

    def test_missing_asset_and_zero_search_matches_are_distinct(self):
        self.assertEqual(self.reader.search_catalog(query='no-such-catalog-item')['status'], 'NO_MATCH')
        with self.assertRaises(MaterialError) as caught:
            self.reader.describe_asset('no-such-catalog-item')
        self.assertEqual(caught.exception.code, 'ASSET_NOT_FOUND')

    def test_search_records_filter_basis_without_asserting_coverage(self):
        result = self.reader.search_catalog(kind='capability', maintainer_role='billing_maintainer', capability_kind='PREPARE')
        self.assertIn('native.prepare', {x['id'] for x in result['matches']})
        for row in result['matches']:
            self.assertTrue(row['match_reasons'])
            self.assertEqual(row['coverage_status'], 'NOT_PROVEN_BY_SEARCH')

    def test_scenario_filter_follows_explicit_relationships(self):
        result = self.reader.search_catalog(scenario_id='terminated_subscription')
        found = {(x['asset_kind'], x['id']) for x in result['matches']}
        self.assertIn(('stage', 'terminated_subscription.initial'), found)
        self.assertIn(('capability', 'sut.submit'), found)
        self.assertIn(('source', 'lago_api'), found)
        self.assertNotIn(('scenario', 'v1_history'), found)
        self.assertNotIn(('capability', 'run.release'), found)

    def test_invalid_filters_fail_instead_of_broadening_search(self):
        for filters in ({'kind': 'arbitrary'}, {'kind': []}, {'capability_kind': []},
                        {'scenario_id': 'missing'}, {'query': []}):
            with self.assertRaises(MaterialError):
                self.reader.search_catalog(**filters)

    def test_reference_only_identity_does_not_read_body(self):
        with patch.object(self.reader.resolver, '_read_verified_bytes', side_effect=AssertionError('BODY_FORBIDDEN')):
            result = self.reader.material_identity('tools/multi-system/release_oracle.py')
        self.assertEqual(result['status'], 'REFERENCE_ONLY')
        self.assertFalse(result['content_projected'])
        self.assertEqual(result['git_commit'], 'cb3881c0a9bfd67fec4ac05f31457b3d95e587b3')
        self.assertEqual(len(result['sha256']), 64)

    def test_gap_ids_and_digest_come_from_pinned_existing_document(self):
        gaps = self.reader.gap_records()
        raw = subprocess.run(['git', 'show', CATALOG_COMMIT + ':' + GAP_PATH], cwd=ROOT,
                             check=True, capture_output=True).stdout
        self.assertEqual(gaps['gaps'], json.loads(raw)['gaps'])
        self.assertEqual(gaps['source']['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(gaps['source']['git_commit'], CATALOG_COMMIT)

    def test_restricted_scope_does_not_claim_full_material_validation(self):
        restricted = replace(self.scope, allow_content=False)
        reader = load_catalog(ROOT, CATALOG_COMMIT, restricted)
        self.assertEqual(reader.validation['status'], 'VALID_DRAFT_METADATA_ONLY')
        self.assertEqual(reader.validation['material_integrity'], 'NOT_FULLY_READ_SCOPE_RESTRICTED')
        material = reader.resolver.read_registered_material('business-source-pack/v1/policies/rules.md')
        self.assertEqual(material['status'], 'METADATA_ONLY')
        self.assertIsNone(material['content'])

    def test_metadata_denial_is_explicit(self):
        with self.assertRaises(MaterialError) as caught:
            load_catalog(ROOT, CATALOG_COMMIT, replace(self.scope, allow_metadata=False))
        self.assertEqual(caught.exception.code, 'ACCESS_DENIED')

    def test_readonly_loading_uses_only_local_git_and_no_product_import(self):
        original_run = subprocess.run
        calls = []
        def local_git_only(args, **kwargs):
            calls.append(args)
            self.assertEqual(args[0], 'git')
            return original_run(args, **kwargs)
        before = set(sys.modules)
        with patch('subprocess.run', side_effect=local_git_only), \
                patch.object(socket, 'create_connection', side_effect=AssertionError('NETWORK_FORBIDDEN')), \
                patch('os.system', side_effect=AssertionError('SCRIPT_FORBIDDEN')):
            reader = load_catalog(ROOT, CATALOG_COMMIT, self.scope)
            reader.search_catalog(business_boundary='订阅')
            reader.describe_asset('sut.submit', kind='capability')
        self.assertTrue(calls)
        self.assertFalse(any(name.startswith(('support_sut', 'commercial_source', 'release_seed', 'release_check'))
                             for name in set(sys.modules) - before))

    def test_cli_error_is_safe_and_unknown_scope_cannot_be_supplied(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            status = main(['--root', str(ROOT), 'describe', 'missing'])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())['error_code'], 'ASSET_NOT_FOUND')

    def test_export_creates_new_private_output_and_never_overwrites(self):
        task = {
            'task_id': 'terminated-subscription-plan-v01',
            'task_mode': 'KNOWN_ISSUE_INVESTIGATION',
            'goal': '调查终止订阅账单争议为何在创建 Job 前失败，并规划验证',
            'catalog_commit': CATALOG_COMMIT,
            'product_candidate_ref': None,
            'historical_issue_ref': 'reports/05-baseline/20261110-release-verification/terminated-investigation.json',
            'operation_scope': 'PLAN_ONLY',
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            task_file = path / 'task.json'
            task_file.write_text(json.dumps(task))
            destination = path / 'new-context'
            args = ['--root', str(ROOT), 'assemble', '--task', str(task_file), '--output-directory', str(destination)]
            with patch('sys.stdout', io.StringIO()):
                self.assertEqual(main(args), 0)
            context = json.loads((destination / 'task-context.json').read_text())
            self.assertIsNone(context['task_identity']['product_candidate_ref'])
            self.assertTrue((destination / 'readiness.json').is_file())
            self.assertTrue((destination / 'gaps.json').is_file())
            self.assertEqual((destination / 'task-context.json').stat().st_mode & 0o777, 0o600)
            before = (destination / 'task-context.json').read_bytes()
            with patch('sys.stdout', io.StringIO()):
                self.assertEqual(main(args), 1)
            self.assertEqual(before, (destination / 'task-context.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
