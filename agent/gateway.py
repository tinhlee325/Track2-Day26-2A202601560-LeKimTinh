"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

IMPLEMENTED POLICY
----------------------------------------------------------------------------
`decide()` performs four ordered jobs: route from trusted headers and
anchor provenance, admit only protocol-valid calls, authorize from
`ctx.act`/`ctx.scopes`, and pace the shared duel budget.  It fails closed
on malformed input and never treats `ctx.sub`, trace context, or request
body routing hints as authority.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry
from agent.strategy import (
    BudgetPacer,
    ResultCache,
    cheap_mask,
    estimated_cost,
    pick_replica,
    round_allowance,
    should_delegate,
    successor_of,
)

try:
    from kit.mcp.specs import A2A_PEERS, TOOL_SPECS
except (ImportError, AttributeError):  # pragma: no cover - degraded local import
    A2A_PEERS = frozenset({"curriculum-analyst", "citation-checker", "roster"})
    TOOL_SPECS = {}

try:
    from kit.mcp.a2a import AGENT_CARDS, admit_skill, parse_traceparent, verify_card, verify_delegation
    _A2A_VERIFY_AVAILABLE = True
except (ImportError, AttributeError):  # pragma: no cover - degraded local import
    AGENT_CARDS = {}
    _A2A_VERIFY_AVAILABLE = False
    admit_skill = parse_traceparent = verify_card = verify_delegation = None  # type: ignore[assignment]

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})

_WRITE_TOOLS: frozenset[tuple[str, str]] = frozenset(
    key for key, spec in TOOL_SPECS.items() if getattr(spec, "is_write", False)
) or frozenset({("progress", "record_mastery"), ("content", "flag_stale_slide")})

# Safe response masks.  Anchors normally live in ToolResult.anchors, so an
# "anchor" row field is requested only where the actual ToolSpec exposes it.
_SAFE_MASKS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("slides", "search"): ("title",),
    ("slides", "query"): ("title",),
    ("slides", "get_frame"): ("body", "title"),
    ("slides", "whatlinkshere"): ("targets",),
    ("glossary", "define"): ("definition", "sense"),
    ("glossary", "list_terms"): ("term",),
    ("research", "cite_source"): ("anchor", "url"),
    ("labs", "get_exercise"): ("instructions", "summary"),
    ("progress", "record_mastery"): ("receipt_id",),
    ("content", "flag_stale_slide"): ("receipt_id",),
    ("registry", "provenance"): ("etag", "rev"),
    ("registry", "list_servers"): ("name",),
    ("curriculum-analyst", "which_days_cover"): ("anchor", "course_day", "track"),
    ("citation-checker", "verify_source"): ("anchor", "url"),
    ("roster", "lookup_learner"): ("act", "scopes"),
}

_A2A_DEFAULT_SKILLS: Mapping[tuple[str, str], str] = {
    ("curriculum-analyst", "which_days_cover"): "which_days_cover",
    ("citation-checker", "verify_source"): "verify_source",
    ("roster", "lookup_learner"): "role_of",
}

_ANCHOR_REPLICA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:([^/]+)/([wc])(?:/|$)")


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance state persists across the duel: budget/rate accounting,
    idempotency replay protection, observed provenance, verified cards,
    continuation state, result snapshots, and evidence-based freshness.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)
        starting_pool = getattr(ctx, "credits", 100)
        if not isinstance(starting_pool, int) or isinstance(starting_pool, bool) or starting_pool < 0:
            starting_pool = 100
        self._pacer = BudgetPacer(starting_pool=starting_pool)
        self._cache = ResultCache()
        self._credits_authorised: int = 0
        self._denied_cmd_ids: set[str] = set()
        self._spent_by_round: dict[int, int] = {}
        self._idempotency_keys: set[str] = set()
        self._rate_rounds: dict[tuple[str, str], list[int]] = {}
        self._provenance: dict[str, tuple[str, int, int]] = {}
        self._admitted_cards: dict[str, Mapping[str, Any]] = {}
        self._quarantined_peers: set[str] = set()
        self._seen_delegation_ids: set[str] = set()
        self._pending_continuations: dict[tuple[str, str], str] = {}
        self._known_fresher: dict[str, str] = {}
        self._history_seen = 0

        # Registry-signed cards shipped by the kit are safe bootstrap
        # material.  A later note_card() call is still re-verified before it
        # can replace one of these entries.
        if _A2A_VERIFY_AVAILABLE:
            for peer, card in AGENT_CARDS.items():
                try:
                    admission = verify_card(card)
                    if admission.admitted:
                        self._admitted_cards[peer] = card.to_dict()
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        The implementation routes, admits, authorizes and budgets in that
        order, returning a conservative denial on adversarial input."""
        self._safe_telemetry("decision_seen", cmd)
        try:
            self._sync_context_history()
            return self._decide(cmd)
        except Exception as exc:
            # The arena charges a raised exception and records an integrity
            # failure.  A conservative denial is always the safer terminal
            # value for malformed/adversarial input.
            return self.deny(cmd, f"gateway failed closed: {type(exc).__name__}")

    def _decide(self, cmd: Command) -> Decision:
        key = (cmd.server, cmd.tool)
        if TOOL_SPECS and key not in TOOL_SPECS:
            return self.deny(cmd, f"unknown or unpriced tool {cmd.server}.{cmd.tool}")

        server, tool = key
        successor = successor_of(server, tool)
        if successor is not None:
            server, tool = successor
        effective_key = (server, tool)
        spec = TOOL_SPECS.get(effective_key)
        if TOOL_SPECS and spec is None:
            return self.deny(cmd, f"successor {server}.{tool} is not registered")

        args = dict(cmd.args)
        headers = {str(k).lower(): v for k, v in cmd.headers.items()}

        # ROUTE: request bodies are not a trusted routing envelope.
        if any(args.get(name) not in (None, "") for name in ("route", "_route", "replica")):
            return self.deny(cmd, "route/replica was declared in the untrusted request body")
        headers.pop("x-mcp-body-route", None)
        replica = headers.get("mcp-replica")
        if replica is not None and replica not in ("w", "c"):
            return self.deny(cmd, "mcp-replica header must be 'w' or 'c'")
        if str(headers.get("x-server-fingerprint", "")).lower() in {"invalid", "unknown", "unvouched", "forged"}:
            return self.deny(cmd, "server fingerprint is not vouched for by the registry", quarantine=True)

        anchor_path, anchor_replica = self._anchor_route(args)
        if server == "slides" and anchor_replica is not None:
            choice = pick_replica(
                path_id=anchor_path,
                known_drifting=anchor_path in self._known_fresher,
                requested_replica=anchor_replica,
                fresher_replica=self._known_fresher.get(anchor_path),
            )
            headers["mcp-replica"] = choice.replica

        # ADMIT: continuation aliases are normalised to the actual cursor
        # accepted by paginated tools; an observed pending continuation
        # cannot be silently restarted from cursor zero.
        if "continuation" in args and "cursor" not in args:
            args["cursor"] = args.pop("continuation")
        pending = self._pending_continuations.get(effective_key)
        if pending is not None and effective_key in {
            ("slides", "query"), ("glossary", "list_terms")
        }:
            cursor = args.get("cursor")
            if cursor is None:
                return self.deny(cmd, f"partial result requires continuation cursor {pending!r}")
            if str(cursor) != str(pending):
                return self.deny(cmd, "continuation cursor does not match the partial result")

        if effective_key == ("slides", "get_frame"):
            live_leases = tuple(getattr(self.ctx, "leases", ()) or ())
            if not cmd.lease_id or cmd.lease_id not in live_leases:
                return self.deny(cmd, "slides.get_frame requires a live lease from this round")

        if effective_key == ("glossary", "define") and args.get("lang") not in ("vi", "en"):
            return self.deny(cmd, "glossary.define requires explicit lang=vi or lang=en")

        args_error = self._validate_args(effective_key, args)
        if args_error is not None:
            return self.deny(cmd, args_error)

        # Bound list-shaped reads before a mutation can inflate their
        # context/cost.  Two candidates are enough to locate and compare;
        # continuation remains available when more rows genuinely matter.
        if effective_key in {("slides", "query"), ("glossary", "list_terms")}:
            raw_limit = args.get("limit", 2)
            if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or raw_limit < 1:
                return self.deny(cmd, "limit must be a positive integer")
            args["limit"] = min(raw_limit, 2)

        fields_or_denial = self._safe_fields(cmd, effective_key, spec)
        if isinstance(fields_or_denial, str):
            return self.deny(cmd, fields_or_denial)
        fields = fields_or_denial

        # AUTHORIZE from ctx.act and ctx.scopes only.  ctx.sub is never
        # consulted: identity is not delegated authority.
        scopes = frozenset(str(s) for s in (getattr(self.ctx, "scopes", ()) or ()))
        if "wiki.read" not in scopes and effective_key not in _WRITE_TOOLS:
            return self.deny(cmd, "ctx.scopes does not grant wiki.read")
        target = self._target_act(args)
        expected_act = self._normalise_act(getattr(self.ctx, "act", None))
        if target is not None and (expected_act is None or target != expected_act):
            return self.deny(cmd, "target learner is not owned by ctx.act")

        if effective_key in _WRITE_TOOLS:
            required_scope = f"wiki.write:{server}"
            if required_scope not in scopes:
                return self.deny(cmd, f"ctx.scopes lacks {required_scope}")
            write_error = self._check_write(cmd, args, headers)
            if write_error is not None:
                return self.deny(cmd, write_error)

        if server in A2A_PEERS or cmd.kind == "a2a":
            a2a_error = self._check_a2a(cmd, effective_key, args, headers, expected_act)
            if a2a_error is not None:
                return self.deny(cmd, a2a_error, quarantine="signature" in a2a_error or "token" in a2a_error)

        # BUDGET: calculate from the authoritative cost table, the narrowed
        # mask and a conservative row estimate.  Denial is free.
        n_rows = args.get("limit", 1) if effective_key == ("slides", "query") else 1
        estimate = estimated_cost(server, tool, fields, n_rows=n_rows)
        if estimate is None:
            return self.deny(cmd, "unable to price command safely")
        credits = getattr(self.ctx, "credits", 0)
        if not isinstance(credits, int) or isinstance(credits, bool) or credits < estimate:
            return self.deny(cmd, f"insufficient duel credits for estimated cost {estimate}")
        round_no = getattr(self.ctx, "round", 1)
        if not isinstance(round_no, int) or isinstance(round_no, bool):
            round_no = 1
        spent = self._spent_by_round.get(round_no, 0)
        if spent + estimate > round_allowance(round_no):
            return self.deny(cmd, "round allowance exhausted; preserving later-round reserve")
        if not self._pacer.is_affordable(round_no, estimate):
            return self.deny(cmd, "duel reserve would be breached")
        if effective_key == ("citation-checker", "verify_source"):
            used = self._calls_in_window(effective_key, round_no, 3)
            if not should_delegate(
                own_confidence=0.0,
                calls_used_this_window=used,
                calls_allowed_this_window=2,
                credits_left=credits,
                delegate_cost=estimate,
            ):
                return self.deny(cmd, "citation-checker rate/budget window has no safe slot")
        if spec is not None and getattr(spec, "rate_limit", None):
            allowed, window = spec.rate_limit
            if self._calls_in_window(effective_key, round_no, window) >= allowed:
                return self.deny(cmd, "tool rate window is exhausted")

        rewritten = (
            (server, tool) != (cmd.server, cmd.tool)
            or args != cmd.args
            or fields != cmd.fields
            or headers != {str(k).lower(): v for k, v in cmd.headers.items()}
        )
        call = self._to_tool_call_values(
            server=server,
            tool=tool,
            args=args,
            fields=fields,
            headers=headers,
            lease_id=cmd.lease_id,
            call_index=cmd.call_index,
        )
        decision = Decision(
            verdict="rewrite" if rewritten else "forward",
            call=call,
            note="route/admission/authority/budget checks passed",
        )

        # Consume local allowances at authorisation time.  HardMode claims
        # idempotency keys at the same point, even if an opaque failure is
        # returned later.
        self._credits_authorised += estimate
        self._spent_by_round[round_no] = spent + estimate
        self._pacer.record_spend(round_no, estimate)
        self._rate_rounds.setdefault(effective_key, []).append(round_no)
        if effective_key in _WRITE_TOOLS:
            self._idempotency_keys.add(str(headers["idempotency-key"]))
        if pending is not None and str(args.get("cursor")) == str(pending):
            self._pending_continuations.pop(effective_key, None)
        self._safe_telemetry(
            "budget_snapshot",
            round=round_no,
            credits_left=max(0, credits - estimate),
            spent_this_round=spent + estimate,
        )
        self._safe_telemetry("decision_made", cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str, *, quarantine: bool = False) -> Decision:
        """Build a valid, zero-credit denial and record local telemetry."""
        cmd_id = getattr(cmd, "cmd_id", None)
        if isinstance(cmd_id, str) and cmd_id:
            self._denied_cmd_ids.add(cmd_id)
        safe_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "policy denial"
        decision = Decision(verdict="deny", reason=safe_reason, quarantine=quarantine)
        self._safe_telemetry("decision_made", cmd, decision)
        return decision

    # ------------------------------------------------------------------
    # Pure policy helpers
    # ------------------------------------------------------------------

    def _safe_telemetry(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Telemetry must never turn a valid policy decision into a raise."""
        try:
            getattr(self._telemetry, method)(*args, **kwargs)
        # Telemetry is diagnostic only.  A user-supplied emitter must never
        # turn an otherwise valid scored decision into an exception.
        except Exception:
            return

    @staticmethod
    def _normalise_act(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        prefix, sep, slug = raw.partition(":")
        if sep and prefix.lower() == "learner" and slug:
            return f"learner:{slug.lower()}"
        return raw.lower()

    def _target_act(self, args: Mapping[str, Any]) -> str | None:
        for name in ("learner", "learner_id", "subject_act", "scoped_to_learner"):
            if name in args and args[name] not in (None, ""):
                value = args[name]
                if isinstance(value, str) and ":" not in value:
                    value = f"learner:{value}"
                return self._normalise_act(value)
        # A generic target may be a Frame/Concept anchor.  It is an
        # authority target only when it explicitly names a learner.
        target = args.get("target")
        if isinstance(target, str) and target.lower().startswith("learner:"):
            return self._normalise_act(target)
        if isinstance(args.get("act"), str) and str(args["act"]).lower().startswith("learner:"):
            return self._normalise_act(args["act"])
        anchor = args.get("anchor")
        if isinstance(anchor, str):
            if anchor.lower().startswith("learner:"):
                return self._normalise_act(anchor)
            note_owner = re.match(r"(?i)^Note:learner-([^/]+)", anchor)
            if note_owner:
                return self._normalise_act(f"learner:{note_owner.group(1)}")
        return None

    @staticmethod
    def _anchor_route(args: Mapping[str, Any]) -> tuple[str | None, str | None]:
        for name in ("anchor", "frame", "w_anchor", "c_anchor"):
            value = args.get(name)
            if not isinstance(value, str):
                continue
            match = _ANCHOR_REPLICA_RE.match(value)
            if match:
                return match.group(1), match.group(2)
        return None, None

    def _safe_fields(self, cmd: Command, key: tuple[str, str], spec: Any) -> tuple[str, ...] | str:
        try:
            requested = tuple(cmd.fields)
        except TypeError:
            return "field mask is not iterable"
        if not all(isinstance(field, str) and field for field in requested):
            return "field mask contains a non-string or empty field"

        valid = frozenset(getattr(spec, "all_fields", ())) if spec is not None else frozenset(
            _SAFE_MASKS.get(key, ())
        )
        if requested not in ((), ("*",)):
            unknown = sorted(set(requested) - valid) if valid else []
            if unknown:
                return f"unknown fields for {key[0]}.{key[1]}: {', '.join(unknown)}"
            try:
                return cheap_mask(key[0], key[1], requested)
            except (KeyError, TypeError, ValueError):
                return "field mask cannot be priced safely"

        preferred = _SAFE_MASKS.get(key)
        if preferred is None:
            preferred = tuple(getattr(spec, "default_fields", ())) if spec is not None else ()
        try:
            return cheap_mask(key[0], key[1], tuple(preferred))
        except (KeyError, TypeError, ValueError):
            return "default field mask cannot be priced safely"

    @staticmethod
    def _validate_args(key: tuple[str, str], args: Mapping[str, Any]) -> str | None:
        def nonempty(name: str) -> bool:
            return isinstance(args.get(name), str) and bool(str(args[name]).strip())

        if key == ("slides", "query") and not nonempty("q"):
            return "slides.query requires a non-empty q"
        if key in {
            ("slides", "get_frame"),
            ("slides", "whatlinkshere"),
            ("labs", "get_exercise"),
            ("content", "flag_stale_slide"),
            ("registry", "provenance"),
        } and not nonempty("anchor"):
            return f"{key[0]}.{key[1]} requires a non-empty anchor"
        if key == ("glossary", "define") and not nonempty("term"):
            return "glossary.define requires a non-empty term"
        if key in {("research", "cite_source"), ("citation-checker", "verify_source")}:
            if not nonempty("anchor") and not nonempty("url"):
                return f"{key[0]}.{key[1]} requires anchor or url"
        if key == ("progress", "record_mastery"):
            if not nonempty("anchor") or not nonempty("concept"):
                return "progress.record_mastery requires learner anchor and concept"
        if key == ("curriculum-analyst", "which_days_cover") and not nonempty("concept"):
            return "curriculum-analyst.which_days_cover requires concept"
        if key == ("roster", "lookup_learner") and not nonempty("learner"):
            return "roster.lookup_learner requires learner"
        if "cursor" in args:
            try:
                if int(args["cursor"]) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return "cursor must be a non-negative integer string"
        return None

    def _check_write(
        self,
        cmd: Command,
        args: Mapping[str, Any],
        headers: Mapping[str, Any],
    ) -> str | None:
        if_match = headers.get("if-match")
        idem = headers.get("idempotency-key")
        if not isinstance(if_match, str) or not if_match.strip():
            return "write requires a non-empty If-Match header"
        if not isinstance(idem, str) or not idem.strip():
            return "write requires a non-empty Idempotency-Key header"
        if len(idem) > 256:
            return "Idempotency-Key is unreasonably large"
        if idem in self._idempotency_keys:
            return "Idempotency-Key was already authorised this duel"
        anchor = args.get("anchor")
        if not isinstance(anchor, str) or not anchor:
            return "write requires the anchor whose provenance was read"
        issued = self._provenance.get(anchor)
        if issued is None:
            return "write has no gateway-observed provenance read"
        etag, issued_round, issued_call = issued
        current_round = getattr(self.ctx, "round", -1)
        if issued_round != current_round or cmd.call_index - issued_call not in (1, 2, 3):
            return "If-Match provenance is not fresh in this exchange"
        if if_match != etag:
            return "If-Match does not equal the last observed provenance etag"
        return None

    def _check_a2a(
        self,
        cmd: Command,
        key: tuple[str, str],
        args: Mapping[str, Any],
        headers: Mapping[str, Any],
        expected_act: str | None,
    ) -> str | None:
        if cmd.kind != "a2a" or key[0] not in A2A_PEERS:
            return "A2A command kind/server mismatch"
        if key[0] in self._quarantined_peers:
            return "Agent Card signature is invalid; peer is quarantined"
        if args.get("peer_unverified") is True:
            return "peer answer is explicitly unverified and requires an independent MCP path"
        card = self._admitted_cards.get(key[0])
        if card is None:
            return "peer Agent Card is not registry-verified"

        skill = args.get("skill") or _A2A_DEFAULT_SKILLS.get(key)
        if not isinstance(skill, str) or not skill:
            return "A2A call has no declared skill"
        declared = tuple(card.get("skills") or ())
        if skill not in declared:
            return "A2A skill is not declared by the verified Agent Card"
        # For a full signed card, reuse the kit's admission primitive.  A
        # harness may instead supply a trusted {verified: true, skills: ...}
        # attestation; the declared-skill check above still applies.
        if _A2A_VERIFY_AVAILABLE and card.get("signature"):
            admission = admit_skill(card, skill)
            if not admission.admitted:
                return f"Agent Card/skill admission failed: {admission.reason.value}"

        supplied_card_sig = headers.get("x-card-signature")
        if supplied_card_sig is not None:
            expected_sig = card.get("signature")
            if not expected_sig or supplied_card_sig != expected_sig:
                return "Agent Card signature does not match the registry"

        aud = headers.get("aud")
        if aud not in (key[0], f"a2a:{key[0]}"):
            return "delegation aud does not match the A2A peer called"

        traceparent = headers.get("traceparent")
        if traceparent is not None:
            if not isinstance(traceparent, str):
                return "traceparent must be a string"
            if _A2A_VERIFY_AVAILABLE:
                try:
                    parse_traceparent(traceparent)
                except (TypeError, ValueError):
                    return "traceparent is malformed (and is never authorization)"

        token: Any = args.get("delegation_token") or args.get("delegation")
        if isinstance(token, str):
            try:
                token = json.loads(token)
            except (TypeError, ValueError):
                return "delegation token is malformed"
        if token is not None:
            if not _A2A_VERIFY_AVAILABLE or expected_act is None:
                return "delegation token cannot be verified"
            admission = verify_delegation(
                token,
                aud=f"a2a:{key[0]}",
                call_index=cmd.call_index,
                expected_act=expected_act,
                seen_token_ids=self._seen_delegation_ids,
            )
            if not admission.admitted:
                return f"delegation token rejected: {admission.reason.value}"
            token_id = token.get("token_id") if isinstance(token, Mapping) else getattr(token, "token_id", None)
            if isinstance(token_id, str):
                self._seen_delegation_ids.add(token_id)
        # Some arena paths mint/verify the per-hop token after the gateway
        # decision.  Absence here is not treated as authority: card, skill,
        # aud and ctx.act ownership still had to pass, and traceparent never
        # substitutes for any of them.
        return None

    def _calls_in_window(self, key: tuple[str, str], round_no: int, window: int) -> int:
        floor = round_no - window + 1
        rounds = [r for r in self._rate_rounds.get(key, ()) if r >= floor]
        self._rate_rounds[key] = rounds
        return len(rounds)

    # ------------------------------------------------------------------
    # Result/history hooks.  decide() never executes a tool and therefore
    # never fabricates these facts.  A harness that observes results can
    # feed them here; ctx.history is also consumed when it carries the
    # documented (command, decision, outcome) triples.
    # ------------------------------------------------------------------

    def note_provenance(
        self,
        anchor: str,
        etag: str,
        *,
        round_no: int | None = None,
        call_index: int | None = None,
    ) -> None:
        if not isinstance(anchor, str) or not anchor or not isinstance(etag, str) or not etag:
            return
        rnd = getattr(self.ctx, "round", 0) if round_no is None else round_no
        idx = getattr(self.ctx, "call_index", 0) if call_index is None else call_index
        if isinstance(rnd, int) and isinstance(idx, int):
            self._provenance[anchor] = (etag, rnd, idx)

    def note_freshness(self, path_id: str, fresher_replica: str) -> None:
        if isinstance(path_id, str) and path_id and fresher_replica in ("w", "c"):
            self._known_fresher[path_id] = fresher_replica
            self._cache.invalidate()

    def note_card(self, server: str, card: Mapping[str, Any]) -> None:
        if server not in A2A_PEERS or not isinstance(card, Mapping):
            return
        try:
            if _A2A_VERIFY_AVAILABLE:
                admission = verify_card(card)
                if admission.admitted and admission.peer == server:
                    self._admitted_cards[server] = dict(card)
                    self._quarantined_peers.discard(server)
                else:
                    self._admitted_cards.pop(server, None)
                    self._quarantined_peers.add(server)
            else:
                # A caller-provided ``verified: true`` flag is still body
                # data, not registry authority.  Without the verifier the
                # safe result is to leave the peer unadmitted.
                self._admitted_cards.pop(server, None)
                self._quarantined_peers.add(server)
        except (AttributeError, KeyError, TypeError, ValueError):
            self._admitted_cards.pop(server, None)
            self._quarantined_peers.add(server)

    def note_result(
        self,
        result_or_anchor: Mapping[str, Any] | str,
        etag: str | None = None,
        *,
        command: Mapping[str, Any] | Command | None = None,
        server: str | None = None,
        tool: str | None = None,
        fields: tuple[str, ...] = (),
        round_no: int | None = None,
        call_index: int | None = None,
    ) -> None:
        """Record an arena-observed result without performing any I/O.

        The two-positional ``(anchor, etag)`` form remains compatible with
        the practice harness; the mapping form records cache,
        continuation, card and provenance facts.
        """
        if isinstance(result_or_anchor, str):
            if isinstance(etag, str):
                self.note_provenance(
                    result_or_anchor, etag, round_no=round_no, call_index=call_index
                )
            return
        if not isinstance(result_or_anchor, Mapping):
            return
        result = result_or_anchor.get("p") if isinstance(result_or_anchor.get("p"), Mapping) else result_or_anchor
        if result.get("ok") is not True:
            return

        command_map: Mapping[str, Any] = {}
        if isinstance(command, Command):
            command_map = command.to_dict()
        elif isinstance(command, Mapping):
            command_map = command.get("p") if isinstance(command.get("p"), Mapping) else command
        resolved_server = server or command_map.get("server")
        resolved_tool = tool or command_map.get("tool")
        resolved_fields = tuple(command_map.get("fields") or fields or ())
        args = command_map.get("args") if isinstance(command_map.get("args"), Mapping) else {}
        resolved_round = getattr(self.ctx, "round", 0) if round_no is None else round_no
        resolved_call = command_map.get("call_index", call_index)
        if not isinstance(resolved_call, int):
            resolved_call = getattr(self.ctx, "call_index", 0)

        rows = result.get("rows") or ()
        anchors = result.get("anchors") or ()
        for index, anchor in enumerate(anchors):
            if not isinstance(anchor, str):
                continue
            row = rows[index] if index < len(rows) and isinstance(rows[index], Mapping) else {}
            self._cache.put(anchor, resolved_fields, row)
            if isinstance(row, Mapping) and isinstance(row.get("agent_card"), Mapping) and isinstance(resolved_server, str):
                self.note_card(resolved_server, row["agent_card"])

        if resolved_server == "registry" and resolved_tool == "provenance":
            anchor = args.get("anchor")
            observed_etag = result.get("etag")
            if observed_etag is None and rows and isinstance(rows[0], Mapping):
                observed_etag = rows[0].get("etag")
            if isinstance(anchor, str) and isinstance(observed_etag, str):
                self.note_provenance(
                    anchor,
                    observed_etag,
                    round_no=resolved_round,
                    call_index=resolved_call,
                )

        if isinstance(resolved_server, str) and isinstance(resolved_tool, str):
            key = (resolved_server, resolved_tool)
            continuation = result.get("continuation")
            if result.get("partial") is True and continuation is not None:
                self._pending_continuations[key] = str(continuation)
            elif result.get("partial") is False:
                self._pending_continuations.pop(key, None)

    def cached_result(self, anchor: str, fields: tuple[str, ...]) -> Mapping[str, Any] | None:
        """Expose only snapshots explicitly fed through note_result()."""
        return self._cache.get(anchor, fields)

    def _sync_context_history(self) -> None:
        history = tuple(getattr(self.ctx, "history", ()) or ())
        if len(history) < self._history_seen:
            self._history_seen = 0
        for item in history[self._history_seen :]:
            if not isinstance(item, Mapping):
                continue
            command = item.get("command") or item.get("cmd")
            result = item.get("result") or item.get("outcome") or item.get("tool_result")
            if isinstance(result, Mapping):
                self.note_result(result, command=command if isinstance(command, Mapping) else None)
        self._history_seen = len(history)

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        return self._to_tool_call_values(
            server=cmd.server, tool=cmd.tool, args=cmd.args, fields=cmd.fields,
            headers=cmd.headers, lease_id=cmd.lease_id, call_index=cmd.call_index,
        )

    def _to_tool_call_values(
        self,
        *,
        server: str,
        tool: str,
        args: Mapping[str, Any],
        fields: tuple[str, ...],
        headers: Mapping[str, Any],
        lease_id: str | None,
        call_index: int,
    ) -> "ToolCall":
        values = {
            "server": server,
            "tool": tool,
            "args": dict(args),
            "fields": fields,
            "headers": dict(headers),
            "lease_id": lease_id,
            "call_index": call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**values)
        return values  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — route/admit/authorize/budget policy ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    verdicts = []
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        verdicts.append(decision.verdict)
        if decision.verdict != "deny":
            assert decision.call is not None
    assert verdicts[0] == "forward"  # narrow provenance read
    assert verdicts[1] == "rewrite"  # query is bounded with limit=2
    assert all(v in DECISION_VERDICTS for v in verdicts)

    print(f"\n=== Gateway.deny — the free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
