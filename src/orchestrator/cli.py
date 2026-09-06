"""Text-first control surface.

This is what gets driven over chat: the relay agent on the Ubuntu host runs
these commands and pastes the output back, so everything here is terse and
readable as plain text rather than JSON. ``--json`` is available when a
machine is reading.

The brief's requirement is ask/check working demonstrably over text before
any audio touches it. This is that surface.
"""

from __future__ import annotations

import argparse
import json
import sys

from .registry import ConfigError, load
from .supervisor import Supervisor

__all__ = ["main"]


def _fmt_check(result: dict) -> str:
    if "error" in result:
        return _fmt_error(result)
    lines = [f"{result['job_id']}  {result['state'].upper()}"]
    for line in result.get("narration", []):
        lines.append(f"  · {line}")
    if not result.get("narration"):
        lines.append("  · (no narration yet)")
    if result.get("detail"):
        lines.append(f"  detail: {result['detail']}")
    if result.get("log_tail"):
        lines.append("  --- log tail ---")
        lines += [f"  {ln}" for ln in result["log_tail"]]
    return "\n".join(lines)


def _fmt_error(result: dict) -> str:
    msg = f"error: {result.get('message', result['error'])}"
    for key in ("known", "candidates"):
        if result.get(key):
            msg += f"\n  {key}: {', '.join(result[key])}"
    return msg


def _fmt_jobs(result: dict) -> str:
    jobs = result["jobs"]
    if not jobs:
        return "no jobs"
    width = max(len(j["job_id"]) for j in jobs)
    return "\n".join(
        f"{j['job_id']:<{width}}  {j['state']:<14} {j['agent'] or '?':<12} {j['repo'] or '-'}"
        for j in jobs
    )


def _fmt_agents(result: dict) -> str:
    return "\n".join(
        f"{a['name']:<14} {a['type']:<12} {a['description']}" for a in result["agents"]
    )


def _fmt_repos(result: dict) -> str:
    return "\n".join(
        f"{r['name']:<14} {r['base_ref']:<10} {', '.join(r['aliases'])}" for r in result["repos"]
    )


def _fmt_credentials(result: dict) -> str:
    items = result["credentials"]
    if not items:
        return f"vault {result['vault']} is empty"
    width = max(len(i["title"]) for i in items)
    return "\n".join(f"{i['title']:<{width}}  ${i['env_var']}" for i in items)


def _fmt_grants(result: dict) -> str:
    grants = result["grants"]
    if not grants:
        return "no credentials are currently delegated"
    width = max(len(g["agent"]) for g in grants)
    return "\n".join(
        f"{g['agent']:<{width}}  {g['credential']:<28} ${g['env_var']:<24} "
        f"{('job ' + g['job_id']) if g['job_id'] else 'until revoked'}"
        for g in grants
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestrate", description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of text")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="start a job")
    p_ask.add_argument("agent")
    p_ask.add_argument("message", nargs="+")
    p_ask.add_argument("--repo", help="repo name, alias, or how you'd say it out loud")

    p_check = sub.add_parser("check", help="report on a job")
    p_check.add_argument("job_id")
    p_check.add_argument("--tail", type=int, default=0, metavar="N",
                         help="also return the last N raw log lines (off by default)")
    p_check.add_argument("--lines", type=int, default=None, metavar="N",
                         help="narration lines to return")

    p_reply = sub.add_parser("reply", help="answer a job that is waiting on you")
    p_reply.add_argument("job_id")
    p_reply.add_argument("message", nargs="+")

    p_jobs = sub.add_parser("list-jobs", help="list jobs, newest first")
    p_jobs.add_argument("--active", action="store_true", help="hide finished jobs")
    p_jobs.add_argument("--limit", type=int, default=20)

    sub.add_parser("list-agents", help="list configured agents")
    sub.add_parser("list-repos", help="list repos and the names you can call them")

    sub.add_parser("list-credentials", help="what the vault is willing to share")
    sub.add_parser("list-grants", help="what each agent can currently reach")

    p_grant = sub.add_parser("grant", help="give an agent access to a credential")
    p_grant.add_argument("agent")
    p_grant.add_argument("credential", nargs="+")
    p_grant.add_argument("--job", help="scope the grant to one job, revoked when it ends")

    p_revoke = sub.add_parser("revoke", help="withdraw a credential from an agent")
    p_revoke.add_argument("agent")
    p_revoke.add_argument("credential", nargs="+")

    p_stop = sub.add_parser("stop", help="stop a running job now")
    p_stop.add_argument("job_id")

    p_reap = sub.add_parser("reap", help="delete a finished job's workspace")
    p_reap.add_argument("job_id")
    p_reap.add_argument("--delete-branch", action="store_true",
                        help="also delete the job branch (kept by default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        sup = Supervisor.create(load())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    match args.command:
        case "ask":
            result = sup.ask(args.agent, " ".join(args.message), repo=args.repo)
            text = _fmt_error(result) if "error" in result else (
                f"{result['job_id']}  started on {result['agent']}"
                + (f" in {result['repo']} ({result['branch']})" if result["repo"] else "")
            )
        case "check":
            result = sup.check(args.job_id, tail=args.tail, narration_lines=args.lines)
            text = _fmt_check(result)
        case "reply":
            result = sup.reply(args.job_id, " ".join(args.message))
            text = _fmt_error(result) if "error" in result else f"{result['job_id']}  resumed"
        case "list-jobs":
            result = sup.list_jobs(active_only=args.active, limit=args.limit)
            text = _fmt_jobs(result)
        case "list-agents":
            result = sup.list_agents()
            text = _fmt_agents(result)
        case "list-repos":
            result = sup.list_repos()
            text = _fmt_repos(result)
        case "list-credentials":
            result = sup.list_credentials()
            text = _fmt_error(result) if "error" in result else _fmt_credentials(result)
        case "list-grants":
            result = sup.list_grants()
            text = _fmt_error(result) if "error" in result else _fmt_grants(result)
        case "grant":
            result = sup.grant(args.agent, " ".join(args.credential), job_id=args.job)
            text = (
                _fmt_error(result)
                if "error" in result
                else f"granted {result['granted']} to {result['agent']} "
                f"as ${result['env_var']} ({result['scope']})"
            )
        case "revoke":
            result = sup.revoke(args.agent, " ".join(args.credential))
            text = (
                _fmt_error(result)
                if "error" in result
                else f"revoked {result['revoked']} from {result['agent']}"
            )
        case "stop":
            result = sup.stop(args.job_id)
            text = (
                _fmt_error(result)
                if "error" in result
                else f"{result['job_id']}  stopped"
                + (
                    f", {result['credentials_released']} credential(s) released"
                    if result["credentials_released"]
                    else ""
                )
            )
        case "reap":
            result = sup.reap(args.job_id, keep_branch=not args.delete_branch)
            text = _fmt_error(result) if "error" in result else f"{args.job_id}  reaped"
        case _:  # pragma: no cover - argparse rejects this first
            return 2

    print(json.dumps(result, indent=2) if args.json else text)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
