#!/usr/bin/env python3
"""外部 API token 泄露事件流程（demo）。

用法：
  python3 token_incident.py start --reason "token pasted into public log"
  python3 token_incident.py scope --incident INC-xxxx --detail "request ids ..."
  python3 token_incident.py vendor --incident INC-xxxx --detail "vendor acknowledged quarantine"
  python3 token_incident.py close --incident INC-xxxx --approve --vendor-confirmed
  python3 token_incident.py show --incident INC-xxxx

这个脚本不会处理真实供应商数据，也不会存储真实 token；它只展示 GRC 自动化里
“立即 containment + 保留证据 + 调查范围 + vendor 协作 + 人工批准 + 审计闭环”的边界。
"""

import argparse
import json

import db
import provider_control


def cmd_start(args):
    db.init_db()
    incident_id, state = provider_control.begin_token_exposure_incident(
        args.reason,
        suspected_since=args.suspected_since,
    )
    # containment 完成后再继续补充调查与 vendor 动作；不等待完整 scope 才 revoke。
    provider_control.record_event(
        incident_id,
        "scope_investigation_started",
        "collect request ids / timestamps / affected data categories",
    )
    provider_control.record_event(
        incident_id,
        "vendor_containment_requested",
        "request provider to freeze/quarantine suspected window while scope is refined",
    )
    print(json.dumps({"incident_id": incident_id, "provider_state": state}, ensure_ascii=False, indent=2))


def cmd_scope(args):
    db.init_db()
    provider_control.record_event(args.incident, "scope_assessed", args.detail)
    print(json.dumps({"incident_id": args.incident, "stage": "scope_assessed"}, ensure_ascii=False))


def cmd_vendor(args):
    db.init_db()
    provider_control.record_event(args.incident, "vendor_acknowledgement", args.detail)
    print(json.dumps({"incident_id": args.incident, "stage": "vendor_acknowledgement"}, ensure_ascii=False))


def cmd_close(args):
    db.init_db()
    state = provider_control.close_incident(
        args.incident,
        human_approved=args.approve,
        vendor_confirmed=args.vendor_confirmed,
    )
    print(json.dumps({"incident_id": args.incident, "provider_state": state}, ensure_ascii=False, indent=2))


def cmd_show(args):
    db.init_db()
    print(json.dumps({
        "provider_state": provider_control.get_state(),
        "events": provider_control.list_events(args.incident),
    }, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Simulated external API token exposure workflow")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start")
    p.add_argument("--reason", required=True)
    p.add_argument("--suspected-since", default="unknown")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("scope")
    p.add_argument("--incident", required=True)
    p.add_argument("--detail", required=True)
    p.set_defaults(func=cmd_scope)

    p = sub.add_parser("vendor")
    p.add_argument("--incident", required=True)
    p.add_argument("--detail", required=True)
    p.set_defaults(func=cmd_vendor)

    p = sub.add_parser("close")
    p.add_argument("--incident", required=True)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--vendor-confirmed", action="store_true")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("show")
    p.add_argument("--incident", required=True)
    p.set_defaults(func=cmd_show)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
