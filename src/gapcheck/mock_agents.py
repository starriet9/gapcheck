"""Scripted stand-ins for a model, for offline tests and dry runs (no model calls).

- "abstain": answers CANNOT_DETERMINE at once, with no evidence.
- "reference": a scripted agent. It calls tools in a fixed order (demographics,
  diagnosis codes, active medications, the open-world observations, notes,
  then prevent_risk with the inputs it will cite) and answers with the R1
  baseline's output for the case. It reads that answer from the case it is
  bound to, so it is a test double, not a system under study. `mutate` and
  `repair` (functions answer -> answer) let tests break the first or the
  repaired answer.
"""
from __future__ import annotations

import json

from . import codes as C
from . import oracle
from .backends import Response, ToolCall

ABSTAIN = {"status": oracle.CANNOT_DETERMINE, "benefit_group": None, "risk_10y": None,
           "missing": [], "evidence": [],
           "note_for_clinician": "No assessment was made. This is a scripted answer."}


def _abstain(messages, tools):
    return Response(json.dumps(ABSTAIN))


class ReferenceAgent:
    def __init__(self, mutate=None, repair=None, text_override=None):
        self.mutate = mutate
        self.repair = repair
        self.text_override = text_override  # list of raw texts to send before the answer
        self.env = None
        self._n = 0

    def bind(self, env) -> None:
        self.env = env
        self._n = 0

    def _answer(self) -> dict:
        from . import baselines
        from .systems import notes_of
        return baselines.r1(self.env.facts(), notes_of(self.env))

    def _calls(self, step: int, names: set[str], ans: dict) -> list[ToolCall]:
        if names == {"prevent_risk"}:
            plan = {0: "prevent"}
        else:
            plan = {0: "records", 1: "observations", 2: "notes", 3: "prevent"}
        kind = plan.get(step)
        calls = []
        if kind == "records":
            codes = sorted(set(C.ASCVD_CONDITIONS) | set(C.ASCVD_PROCEDURES) | set(C.DIABETES)
                           | set(C.CKD_STAGES))
            calls = [ToolCall("c1", "get_demographics", {}),
                     ToolCall("c2", "search_conditions", {"codes": codes}),
                     ToolCall("c3", "get_medications", {"active_only": True})]
        elif kind == "observations":
            calls = [ToolCall(f"o{i}", "get_observations", {"code": c.code})
                     for i, c in enumerate((C.TC, C.HDL, C.LDL, C.SBP, C.SMOKING, C.EGFR))]
        elif kind == "notes":
            calls = [ToolCall("n1", "search_notes", {"query": "statin"})]
        elif kind == "prevent":
            comp = [it for it in ans["evidence"] if it["support"] == "COMPUTED"
                    and it["claim"] == "risk_10y"]
            calls = [ToolCall(f"p{i}", "prevent_risk", dict(it["inputs"]))
                     for i, it in enumerate(comp)]
        return [c for c in calls if c.name in names]

    def __call__(self, messages, tools):
        names = {t["name"] for t in tools or []}
        last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
        said = last_user["content"] if last_user else ""
        repairing = "evidence checks failed" in said
        ans = self._answer()
        if not repairing:
            if self.text_override:
                return Response(self.text_override.pop(0))
            while self._n < 4 and "not a valid answer" not in said:
                calls = self._calls(self._n, names, ans)
                self._n += 1
                if calls:
                    return Response("", calls)
            out = self.mutate(ans) if self.mutate else ans
        else:
            out = self.repair(ans) if self.repair else (self.mutate(ans) if self.mutate else ans)
        return Response(json.dumps(out))


def policy(name: str):
    if name == "abstain":
        return _abstain
    if name == "reference":
        return ReferenceAgent()
    raise ValueError(f"unknown mock policy {name!r}")
