"""Scripted stand-ins for a model, for offline tests and dry runs (no model calls).

- "abstain": answers CANNOT_DETERMINE at once, with no evidence.
- "reference": a scripted agent. It calls tools in a fixed order (demographics,
  diagnosis codes, active medications, the open-world observations, notes,
  then prevent_risk with the inputs it will cite) and answers with the R1
  baseline's output for the case. It reads that answer from the case it is
  bound to, so it is a test double, not a system under study. `mutate` and
  `repair` (functions answer -> answer) let tests break the first or the
  repaired answer.
- "oracle_complete": a scripted agent that answers with the oracle's label for
  the case (planted reasons read from the case's ledger) and COMPLETE, correct
  evidence: every input claim (age, sex and each closed- and open-world input)
  as DOCUMENTED (the oracle's row ids and values) or ABSENT (named list), every
  reason in the 365-day window as DOCUMENTED (a note reason with the planted
  sentence as its quote), an ABSENT reasons_365d claim when there is none, and
  the PREVENT risks as COMPUTED items taken from real prevent_risk tool calls.
  When smoking status is absent and the row needs PREVENT (C2b), it lists
  smoking in `missing` and makes the two prevent_risk calls (smoker 0 and 1)
  of the two-call rule. It is a fairness check of the verifier: A2 with this
  policy must pass the verifier on every case with no forced fallback. It
  does not read the verifier's required-input table.
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


# ---------------------------------------------------------------- oracle_complete
_PREVENT_LABEL_ROWS = frozenset(r for r, ins in oracle.ROW_INPUTS.items() if "prevent" in ins)
_GAP_ROWS = frozenset(r.id for r in oracle.ROWS if r.label == oracle.GAP)
_CLAIM_ORDER = ("age", "sex", "statins", "hiv", "esrd", "ascvd", "diabetes", "ckd",
                "row5_features", "bp_drugs", "lipid_panel", "sbp", "smoking", "egfr")


def oracle_answer_parts(env) -> tuple[oracle.OracleResult, dict[str, str]]:
    """Oracle label of the bound case (planted reasons from the ledger) and the planted
    sentence of each note (note id -> text)."""
    from . import cases
    if env.case_ref is None:
        raise RuntimeError("oracle_complete needs a CaseEnv built by CaseEnv.from_case")
    case_dir, cid = env.case_ref
    f = env.facts()
    r = oracle.label(f, reasons=cases.case_reasons(case_dir, cid) + oracle.reasons_from_facts(f))
    plants = {p.note_id: p.text for p in cases.case_plants(case_dir, cid)}
    return r, plants


class OracleCompleteAgent:
    """Oracle status with complete evidence; PREVENT risks from real tool calls."""

    def __init__(self):
        self.env = None

    def bind(self, env) -> None:
        self.env = env
        self.r, self.plants = oracle_answer_parts(env)
        f = env.facts()
        self.row = None
        if self.r.status != oracle.CANNOT_DETERMINE:
            self.row = self.r.row or self.r.row_set[0]  # e.g. rows 8 and 9 both GAP
        self.calls = []
        if self.row in _PREVENT_LABEL_ROWS:
            from .baselines import _prevent_args
            args = _prevent_args(f)
            if f.smoking is None:  # two-call rule: smoker 0 and 1, other inputs equal
                self.calls = [{**args, "smoker": 0}, {**args, "smoker": 1}]
            else:
                self.calls = [args]

    def _evidence(self, risks: list[dict]) -> list[dict]:
        from . import schema as S
        from .baselines import _value
        f, r = self.env.facts(), self.r
        src = oracle._sources(f, _CLAIM_ORDER)
        names = ("statins",) if self.row == "0" else _CLAIM_ORDER
        ev = []
        for name in names:
            if name in ("age", "sex"):
                ev.append(S.item(name, S.DOCUMENTED, [S.DEMOGRAPHICS], getattr(f, name)))
            elif src.get(name):
                ev.append(S.item(name, S.DOCUMENTED, list(src[name]), _value(f, name)))
            else:
                ev.append(S.item(name, S.ABSENT, S.ABSENT_LIST_OF[name], None))
        for res in risks:
            ev.append(S.item("risk_10y", S.COMPUTED, S.PREVENT_TOOL, res["risk_10y"],
                             inputs=dict(res["inputs"])))
            if len(risks) == 1 and res.get("risk_30y") is not None:
                ev.append(S.item("risk_30y", S.COMPUTED, S.PREVENT_TOOL, res["risk_30y"],
                                 inputs=dict(res["inputs"])))
        for rs in r.reasons_in_window:
            if rs.source in self.plants:
                ev.append(S.item("reason", S.DOCUMENTED, rs.source, rs.category,
                                 quote=self.plants[rs.source], date=rs.date.isoformat()))
            else:
                ev.append(S.item("reason", S.DOCUMENTED, [rs.source], rs.category,
                                 date=rs.date.isoformat()))
        if not r.reasons_in_window and self.row != "0":
            ev.append(S.item("reason", S.ABSENT, "reasons_365d", None))
        return ev

    def answer(self, risks: list[dict]) -> dict:
        from . import schema as S
        r = self.r
        return S.validate({
            "status": r.status, "benefit_group": self.row,
            "risk_10y": risks[0]["risk_10y"] if len(risks) == 1 else None,
            "missing": list(r.absent), "evidence": self._evidence(risks),
            "note_for_clinician": "Scripted answer with the oracle's label and complete "
                                  "evidence. It is a verifier fairness check."})

    def __call__(self, messages, tools):
        names = {t["name"] for t in tools or []}
        done = [m for m in messages if m["role"] == "tool" and m["name"] == "prevent_risk"]
        if self.calls and not done and "prevent_risk" in names:
            return Response("", [ToolCall(f"pr{i}", "prevent_risk", {
                **a, "sources": {"age": "demographics", "sex": "demographics"}})
                for i, a in enumerate(self.calls)])
        risks = []
        for m in done[-len(self.calls):] if self.calls else []:
            res = json.loads(m["content"])
            if "error" in res:
                raise RuntimeError(f"prevent_risk failed: {res['error']}")
            risks.append(res)
        return Response(json.dumps(self.answer(risks)))


def policy(name: str):
    if name == "abstain":
        return _abstain
    if name == "reference":
        return ReferenceAgent()
    if name == "oracle_complete":
        return OracleCompleteAgent()
    raise ValueError(f"unknown mock policy {name!r}")
