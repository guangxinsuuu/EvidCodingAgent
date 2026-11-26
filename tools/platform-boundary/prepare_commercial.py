#!/usr/bin/env python3
"""已登记原生对象 → 独立规则原件 → 商业状态机，复用既有准备协议。

这是已知原生种子的迁移校准。历史用量事件仅引用原准备回执，不冒充本轮新发送
或迟到实验。来源实例、完整对象范围和规则版本冻结；所有命令沿用原操作 key。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tools/product-boundary"), str(ROOT / "tools/multi-system"), str(ROOT / "tools/source-platform")]
from bind_platform import binding, private, digest
from commercial_attachment import validate as validate_source
from client import SourceClient, SourceClientError
from release_seed import SourceObjects, scenario_specs
from seed import SeedError, save_private, sha
from business_source import dt, message_ref
from tools.registry.reference_resolver import LocalGitReader


class PublishedRules:
    """每次读取均走实时对象 ACL；Git 固定 blob 只校验身份/摘要，不替代网络读取。"""
    def __init__(self, config, repo, commit, directory):
        self.client = SourceClient.from_file(config); self.git = LocalGitReader(repo); self.commit = commit; self.directory = directory
        if self.client.config["principal_id"] != "lab:product-reader" \
                or self.git._git("config", "--get", "remote.origin.url").decode().strip() != "https://github.com/guangxinsuuu/EvidBusinessRules.git" \
                or self.git._git("rev-parse", commit + "^{commit}").decode().strip() != commit:
            raise SeedError("REGISTERED_BUSINESS_RULE_READER_REQUIRED")
        self.catalog = {r["path"]: r for r in json.loads((ROOT / "config/source-publications.v1.json").read_text())["objects"] if r["repository"] == "EvidBusinessRules"}

    def __call__(self, path):
        relative = str(path.relative_to(ROOT / "business-source-pack/v1")); item = self.catalog[relative]
        raw = self.git.read(self.commit, relative)
        source = self.client.read(item["namespace"], item["object_id"], item["version"], expected_sha256=hashlib.sha256(raw).hexdigest())
        expected = {"repository_url": "https://github.com/guangxinsuuu/EvidBusinessRules.git", "commit": self.commit, "path": relative,
            "blob": self.git._git("rev-parse", self.commit + ":" + relative).decode().strip()}
        if source["publisher_id"] != "lab:business-publisher" or source["metadata"]["source"] != expected:
            raise SeedError("BUSINESS_PUBLICATION_IDENTITY_MISMATCH")
        # 同一原件的多次 ACL 读取也分别留回执；重新准备不覆盖前一次观察。
        save_private(self.directory / "rule-reads" / (item["object_id"] + "-" + uuid4().hex + ".json"), source)
        return source["content"]


class ExistingNativeFacts(SourceObjects):
    """复用 prepare_case/command 的幂等协议；只新增现有原生种子的事实采集装配。"""
    def __init__(self, args):
        platform_dir, platform = binding(args.platform_repo, args.platform_project)
        self.source_dir = args.platform_repo / ".local" / args.commercial_project
        self.receipt = private(self.source_dir / "deployment-receipt.json"); validate_source(self.receipt)
        scope_set = private(self.source_dir / "scope-set.json")
        if scope_set["native_platform_project"] != platform["project"] or scope_set["native_platform_manifest_sha256"] != platform["manifest_sha256"] \
                or scope_set["native_seed_sha256"] != digest(args.seed.read_bytes()) \
                or scope_set["native_enrollment_sha256"] != digest(args.enrollment.read_bytes()):
            raise SeedError("COMMERCIAL_NATIVE_BINDING_MISMATCH")
        seed = private(args.seed); self.seed_journal = private(args.seed.with_name("seed-state.json"))
        if self.seed_journal["run_id"] != seed["run_id"]:
            raise SeedError("NATIVE_SEED_JOURNAL_MISMATCH")
        observer = private(platform_dir / "observer-access.private.json")
        preparer = private(self.source_dir / "preparer-client.private.json")
        if observer["role"] != "OBSERVER" or observer["base_url"] != platform["deployment"]["endpoints"]["access"] \
                or preparer["role"] != "PREPARER" or preparer["instance_id"] != args.commercial_project:
            raise SeedError("PREPARATION_ROLES_MISMATCH")
        self.rule_loader = PublishedRules(args.rules_reader_config, args.rules_repo, args.rules_commit, args.output)
        specs = scenario_specs([{"label": c["label"], "tenant_id": c["tenant_id"], "lifecycle": "terminated" if c["label"] == "a_closed" else "renewal"}
            for c in seed["bindings"]["cases"]])
        runtime = {"chatwoot": {"base_url": observer["base_url"], "api_token": observer["token"], "account_id": seed["chatwoot_account_id"]},
            "lago": {"base_url": observer["base_url"], "api_key": observer["token"]}}
        ready = {"base_url": preparer["base_url"], "instance_id": args.commercial_project,
            "source_digest": self.receipt["image_id"], "api_version": "commercial-source-v1"}
        super().__init__(args.output, native={**seed, "scenario_specs": specs}, runtime=runtime, ready=ready,
            private={"admin_token": preparer["token"]}, asset_loader=self.rule_loader)
        identity = {"source": self.receipt, "platform": {k: v for k, v in platform.items() if k != "egap_commit"},
            "rules_commit": args.rules_commit, "seed_journal_sha256": digest(args.seed.with_name("seed-state.json").read_bytes())}
        if self.state.get("deployment_binding", identity) != identity:
            raise SeedError("PREPARATION_DEPLOYMENT_CHANGED")
        self.state["deployment_binding"] = identity
        self.save()

    def plan_existing(self, case):
        """重新读取原件，冻结明确的原生期间；不移动日期、补造发票字段或重发旧事件。"""
        label = case["label"]
        if label in self.state["plans"]:
            return self.state["plans"][label]
        customer = next(c for c in self.native["customers"] if c["tenant_id"] == case["tenant_id"])
        sub = self.native["historical_sample"] if label == "a_closed" else next(s for s in customer["subscriptions"] if s["external_id"] == case["subscription_id"])
        invoice_path = "/api/v1/invoices/" + case["invoice_id"]
        invoice = self.lago.request("GET", invoice_path)["invoice"]
        invoice_receipt = {**self.state["receipts"][-1], "invoice": invoice}
        fees = [r for r in invoice["fees"] if r.get("external_subscription_id") == case["subscription_id"]]
        charges = [r for r in fees if r.get("item", {}).get("type") == "charge"]
        selected = charges or [r for r in fees if r.get("item", {}).get("type") == "subscription"]
        spans = {(dt(r["from_date"]), dt(r["to_date"])) for r in selected}
        if len(spans) != 1:
            raise SeedError("NATIVE_INVOICE_PERIOD_AMBIGUOUS")
        start, end = next(iter(spans)); terminated = label == "a_closed"
        usage_path = "/api/v1/customers/" + customer["external_customer_id"] + ("/past_usage" if terminated else "/current_usage")
        query = {"external_subscription_id": case["subscription_id"], **({"periods_count": 12} if terminated else {})}
        response = self.lago.request("GET", usage_path + "?" + urlencode(query))
        usage = next(r for r in response["usage_periods"] if r["lago_invoice_id"] == case["invoice_id"]) if terminated else response["customer_usage"]
        usage_receipt = {**self.state["receipts"][-1], "path": usage_path, "query": query,
            "scope_kind": "INVOICE_PERIOD" if terminated else "SUBSCRIPTION_CURRENT_PERIOD"}
        if not terminated and (dt(usage["from_datetime"]), dt(usage["to_datetime"])) != (start, end):
            raise SeedError("NATIVE_USAGE_PERIOD_NOT_ALIGNED")
        messages = self.cw.request("GET", self.prefix + f"/conversations/{case['conversation_id']}/messages")["payload"]
        original = next(m for m in messages if m["id"] == sub["customer_message_id"])
        events, event_receipts = [], {}
        for key, operation in self.seed_journal["operations"].items():
            event = operation.get("response", {}).get("event")
            if not isinstance(event, dict) or event.get("external_subscription_id") != case["subscription_id"] or event.get("code") != self.native["metric_code"]:
                continue
            if operation.get("state") != "COMPLETE":
                raise SeedError("NATIVE_EVENT_NOT_CONFIRMED")
            events.append({"event_id": event["transaction_id"], "units": str(event["properties"]["units"]),
                "occurred_at": event["timestamp"], "native_occurred_at": event["timestamp"]})
            event_receipts[event["transaction_id"]] = {"kind": "ORIGINAL_SEED_ACCEPTED_EVENT_RECEIPT", "operation_key": key,
                "seed_journal_sha256": self.state["deployment_binding"]["seed_journal_sha256"], "original_operation": operation}
        claim = end if terminated else start + timedelta(days=1)
        p = {"label": label, "tenant_id": case["tenant_id"], "customer_id": customer["external_customer_id"],
            "subscription_id": case["subscription_id"], "invoice_id": case["invoice_id"], "account_id": self.native["chatwoot_account_id"],
            "conversation_id": case["conversation_id"], "start": start.isoformat(), "end": end.isoformat(), "claimed_at": claim.isoformat(),
            "spec": self.specs[label], "invoice_receipt": invoice_receipt, "usage": usage, "usage_receipt": usage_receipt,
            "native_invoice_kind": "TERMINATION" if terminated else "INITIAL", "original_message": message_ref(original),
            "expected_events": events, "event_receipts": event_receipts, "contract_id": self.native["run_id"] + "-contract-" + label,
            "claim_id": self.native["run_id"] + "-claim-" + label, "coverage_id": self.native["run_id"] + "-coverage-" + label}
        self.state["plans"][label] = p; self.save(); return p

    def prepare_all(self):
        for tenant in ("tenant-a", "tenant-b"):
            for version in ("lab-v1", "lab-v2"):
                self.policy(tenant, version)
        for case in self.native["bindings"]["cases"]:
            label = case["label"]; p = self.plan_existing(case)
            self.prepare_case(label, p)
            if label == "a_closed":
                continue
            if p["expected_events"]:
                for event in p["expected_events"]:
                    base = {"tenant_id": p["tenant_id"], "coverage_id": p["coverage_id"], "event_id": event["event_id"]}
                    self.command(label + ":import-original-send", "record_usage_sent", {**base, "receipt": p["event_receipts"][event["event_id"]]})
                    self.command(label + ":import-original-receive", "record_usage_received", {**base, "receipt": p["event_receipts"][event["event_id"]]})
                self._reconcile_and_seal(label, p, p["usage"], p["usage_receipt"], "original-seed-events")
        self.state["status"] = "KNOWN_NATIVE_FACTS_PREPARED"
        self.state["qualification"] = "BACKFILLED_KNOWN_SEED_RECEIPTS_NOT_LATE_EVENT_EXPERIMENT"
        self.save()
        return {"status": self.state["status"], "source_instance": self.ready["instance_id"], "plans": len(self.state["plans"]),
            "commands": len(self.state["commands"]), "business_final_qualification": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for flag in ("platform-repo", "seed", "enrollment", "rules-reader-config", "rules-repo", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    for flag in ("platform-project", "commercial-project", "rules-commit"):
        parser.add_argument("--" + flag, required=True)
    try:
        args = parser.parse_args()
        if "docs" in args.output.resolve().parts or args.output.name.lower() == "readme.md": raise SeedError("OUTPUT_FORBIDDEN")
        print(json.dumps(ExistingNativeFacts(args).prepare_all()))
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "error_code": str(error) if isinstance(error, (SeedError, SourceClientError)) else type(error).__name__}))
        raise SystemExit(2)
