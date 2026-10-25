"""Retrieval eval: `mf-faq eval --retrieval-only`, and the swap gate of the index build (P3.5).

Every question goes through the real pre-LLM path: scheme resolver (threads keep their active
scheme turn to turn) → not-found layers A–D → hybrid search. Query sets:

- `golden.yaml`: its `answer`, `not_found` and `multi_scheme` cases (refusal, performance, PII
  and greeting cases belong to the Phase 4 router and are skipped here);
- `retrieval.yaml`: the research paraphrase, held-out, holdings and out-of-corpus sets.

An `answer` case is a hit at k when one of the top k chunks is from the expected page, holds the
expected fact and contains every `must_contain` value. Reported per set: hit@1, hit@k, MRR,
false not-founds (and the layer that fired), clarifications; for out-of-corpus sets the share
stopped before the LLM and by which layer; top-cosine ranges for τ (P3.6); latency p50 / p95.

Gates (implementation plan, Phase 3 exit criteria): hit@k ≥ 95% on golden, paraphrase and
held-out (hit@1 ≥ 85% on golden), every holdings case found, ≥ 95% of out-of-corpus questions
stopped by layers A–D, 0 false not-founds, retrieval p95 < 300 ms. Near-misses are reported only:
retrieval can't tell them apart (research §3.5), the LLM does (Phase 5).
"""

import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from mf_faq.ingest.index import IndexGate, LoadedIndex
from mf_faq.ingest.registry import SchemeCatalog
from mf_faq.pipeline.retrieve import Evidence, QueryEncoder, Retriever, Status
from mf_faq.pipeline.schemes import Reason, SchemeResolver

GOLDEN_FILE = "golden.yaml"
RETRIEVAL_FILE = "retrieval.yaml"
IN_CORPUS_SETS = ("golden", "paraphrase", "heldout", "holdings")
OUT_OF_CORPUS_SETS = ("golden_not_found", "ooc_scheme", "ooc_general")
REPORT_ONLY_SETS = ("near_miss",)

MIN_HIT_AT_K = 0.95  # golden, paraphrase, held-out
MIN_GOLDEN_HIT_AT_1 = 0.85
MIN_HOLDINGS_HIT_AT_K = 1.0
MIN_STOPPED = 0.95  # out-of-corpus questions stopped by layers A–D
MAX_P95_MS = 300.0
TAU_MARGIN = (0.03, 0.05)  # P3.6: τ = in-corpus minimum cosine minus this margin

_EVAL_KINDS = {"answer", "not_found", "needs_scheme", "multi_scheme"}


@dataclass(frozen=True)
class Case:
    set: str
    id: str
    q: str
    expect_kind: str
    expect_source: str | None = None
    fact: str | None = None
    must_contain: tuple[str, ...] = ()
    thread: str | None = None  # golden thread id: turns share the active scheme


@dataclass
class Outcome:
    case: Case
    evidence: Evidence
    rank: int | None = None  # 0-based rank of the first correct chunk (answer cases)

    @property
    def passed(self) -> bool:
        kind = self.case.expect_kind
        if kind == "answer":
            return self.rank is not None  # among the top k retrieved
        if kind == "not_found":
            return self.evidence.status is Status.INSUFFICIENT
        return self.evidence.status.value == kind


@dataclass
class SetReport:
    name: str
    n: int
    metrics: dict
    misses: list[dict] = field(default_factory=list)


@dataclass
class RetrievalReport:
    sets: dict[str, SetReport]
    cosine: dict
    latency_ms: dict
    multi_scheme: dict
    failures: list[str]
    top_k: int
    threshold: float

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "top_k": self.top_k,
            "threshold": self.threshold,
            "sets": {k: {"n": v.n, **v.metrics, "misses": v.misses} for k, v in self.sets.items()},
            "cosine": self.cosine,
            "latency_ms": self.latency_ms,
            "multi_scheme": self.multi_scheme,
        }


def _case(set_name: str, raw: dict, thread: str | None = None, case_id: str | None = None):
    return Case(
        set=set_name,
        id=case_id or raw["id"],
        q=raw["q"],
        expect_kind=raw["expect_kind"],
        expect_source=raw.get("expect_source"),
        fact=raw.get("fact"),
        must_contain=tuple(raw.get("must_contain", ())),
        thread=thread,
    )


def load_cases(eval_dir: Path) -> list[Case]:
    """Golden cases in file order (thread turns kept together), then the retrieval sets."""
    cases: list[Case] = []
    golden = yaml.safe_load((eval_dir / GOLDEN_FILE).read_text(encoding="utf-8"))
    for raw in golden:
        turns = raw.get("turns")
        for n, turn in enumerate(turns or [raw], start=1):
            kind = turn["expect_kind"]
            name = {"answer": "golden", "not_found": "golden_not_found"}.get(kind, f"golden_{kind}")
            case_id = f"{raw['id']}.{n}" if turns else raw["id"]
            cases.append(_case(name, turn, raw["id"] if turns else None, case_id))
    sets = yaml.safe_load((eval_dir / RETRIEVAL_FILE).read_text(encoding="utf-8"))
    for name, raws in sets.items():
        cases += [_case(name, raw) for raw in raws]
    return cases


def _rank(case: Case, evidence: Evidence) -> int | None:
    for rank, hit in enumerate(evidence.hits):
        chunk = hit.chunk
        if (
            chunk.source_id == case.expect_source
            and case.fact in chunk.facts
            and all(v in chunk.content for v in case.must_contain)
        ):
            return rank
    return None


def run_cases(retriever: Retriever, resolver: SchemeResolver, cases: list[Case]) -> list[Outcome]:
    outcomes: list[Outcome] = []
    active: dict[str, str | None] = {}
    for case in cases:
        resolution = resolver.resolve(case.q, active.get(case.thread) if case.thread else None)
        # The thread's active scheme moves to the one scheme named or carried over (arch §6.4).
        followed = resolution.reason in (Reason.MATCHED, Reason.CARRY_OVER)
        if case.thread and followed and resolution.scheme_id:
            active[case.thread] = resolution.scheme_id
        if case.expect_kind not in _EVAL_KINDS:
            continue  # router cases (Phase 4); they still moved the thread's scheme above
        evidence = retriever.evidence(case.q, resolution)
        rank = _rank(case, evidence) if case.expect_kind == "answer" else None
        outcomes.append(Outcome(case, evidence, rank))
    return outcomes


def _miss(o: Outcome) -> dict:
    ev = o.evidence
    return {
        "id": o.case.id,
        "q": o.case.q,
        "status": ev.status.value,
        "reason": ev.reason.value if ev.reason else None,
        "detail": list(ev.detail),
        "schemes": list(ev.scheme_ids),
        "top": [h.chunk.chunk_id for h in ev.hits],
    }


def _ratio(num: int, den: int) -> float:
    return round(num / den, 3) if den else 1.0


def summarize(outcomes: list[Outcome], *, top_k: int, threshold: float) -> RetrievalReport:
    by_set: dict[str, list[Outcome]] = {}
    for o in outcomes:
        by_set.setdefault(o.case.set, []).append(o)

    sets: dict[str, SetReport] = {}
    in_cosines: list[float] = []
    for name in IN_CORPUS_SETS:
        rows = by_set.get(name, [])
        answers = [o for o in rows if o.case.expect_kind == "answer"]
        ranks = [o.rank for o in answers]
        for o in answers:
            if o.evidence.search:
                in_cosines.append(o.evidence.search.top_cosine)
        false_nf = [o for o in answers if o.evidence.status is Status.INSUFFICIENT]
        metrics = {
            "hit@1": _ratio(sum(r == 0 for r in ranks), len(ranks)),
            f"hit@{top_k}": _ratio(sum(r is not None and r < top_k for r in ranks), len(ranks)),
            "mrr": round(sum(1 / (r + 1) for r in ranks if r is not None) / len(ranks), 3)
            if ranks
            else 1.0,
            "false_not_found": len(false_nf),
            "false_not_found_by_layer": dict(
                Counter(o.evidence.reason.value for o in false_nf if o.evidence.reason)
            ),
            "other_expected_ok": _ratio(
                sum(o.passed for o in rows if o not in answers), len(rows) - len(answers)
            ),
        }
        sets[name] = SetReport(name, len(rows), metrics, [_miss(o) for o in rows if not o.passed])

    out_cosines: list[float] = []
    for name in (*OUT_OF_CORPUS_SETS, *REPORT_ONLY_SETS):
        rows = by_set.get(name, [])
        stopped = [o for o in rows if o.evidence.status is Status.INSUFFICIENT]
        for o in rows:
            if o.evidence.search and name != "near_miss":
                out_cosines.append(o.evidence.search.top_cosine)
        metrics = {
            "stopped": _ratio(len(stopped), len(rows)),
            "by_layer": dict(Counter(o.evidence.reason.value for o in stopped)),
            "other_status": dict(
                Counter(o.evidence.status.value for o in rows if o not in stopped)
            ),
        }
        sets[name] = SetReport(name, len(rows), metrics, [_miss(o) for o in rows if not o.passed])

    multi = [o for o in outcomes if o.case.expect_kind == "multi_scheme"]
    multi_report = {"n": len(multi), "detected": sum(o.passed for o in multi)}

    latencies = [o.evidence.search.latency_ms for o in outcomes if o.evidence.search]
    latency = {
        "n": len(latencies),
        "p50": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95": round(float(np.percentile(latencies, 95)), 1) if latencies else 0.0,
        "max": round(max(latencies), 1) if latencies else 0.0,
    }
    cosine: dict = {}
    if in_cosines:
        low = min(in_cosines)
        cosine["in_corpus"] = {
            "n": len(in_cosines),
            "min": round(low, 3),
            "p5": round(float(np.percentile(in_cosines, 5)), 3),
            "median": round(statistics.median(in_cosines), 3),
        }
        cosine["tau_range"] = [round(low - TAU_MARGIN[1], 3), round(low - TAU_MARGIN[0], 3)]
    if out_cosines:
        cosine["out_of_corpus_retrieved"] = {
            "n": len(out_cosines),
            "max": round(max(out_cosines), 3),
            "median": round(statistics.median(out_cosines), 3),
        }

    failures: list[str] = []
    k = f"hit@{top_k}"
    for name in ("golden", "paraphrase", "heldout"):
        if name in sets and sets[name].metrics[k] < MIN_HIT_AT_K:
            failures.append(f"{name}: {k} {sets[name].metrics[k]:.3f} < {MIN_HIT_AT_K}")
    if "golden" in sets and sets["golden"].metrics["hit@1"] < MIN_GOLDEN_HIT_AT_1:
        failures.append(
            f"golden: hit@1 {sets['golden'].metrics['hit@1']:.3f} < {MIN_GOLDEN_HIT_AT_1}"
        )
    if "holdings" in sets and sets["holdings"].metrics[k] < MIN_HOLDINGS_HIT_AT_K:
        failures.append(f"holdings: {k} {sets['holdings'].metrics[k]:.3f} < 1.0")
    for name in IN_CORPUS_SETS:
        if name in sets:
            if sets[name].metrics["false_not_found"]:
                failures.append(
                    f"{name}: {sets[name].metrics['false_not_found']} false not-found "
                    f"{sets[name].metrics['false_not_found_by_layer']}"
                )
            if sets[name].metrics["other_expected_ok"] < 1.0:
                failures.append(f"{name}: clarify/multi-scheme cases not handled as expected")
    ooc = [o for name in OUT_OF_CORPUS_SETS for o in by_set.get(name, [])]
    stopped = _ratio(sum(o.evidence.status is Status.INSUFFICIENT for o in ooc), len(ooc))
    if stopped < MIN_STOPPED:
        failures.append(f"out-of-corpus: {stopped:.3f} stopped before the LLM < {MIN_STOPPED}")
    if multi_report["detected"] < multi_report["n"]:
        failures.append(f"multi-scheme: {multi_report['detected']}/{multi_report['n']} detected")
    if latency["p95"] > MAX_P95_MS:
        failures.append(f"latency p95 {latency['p95']} ms > {MAX_P95_MS} ms")
    return RetrievalReport(sets, cosine, latency, multi_report, failures, top_k, threshold)


def evaluate(retriever: Retriever, catalog: SchemeCatalog, eval_dir: Path) -> RetrievalReport:
    outcomes = run_cases(retriever, SchemeResolver(catalog), load_cases(eval_dir))
    return summarize(outcomes, top_k=retriever.top_k, threshold=retriever.threshold)


def retrieval_gate(
    encoder: QueryEncoder,
    catalog: SchemeCatalog,
    eval_dir: Path,
    *,
    query_instruction: str,
    top_k: int,
    threshold: float,
) -> IndexGate:
    """An index-build gate (P3.1): the candidate index must pass this eval to go live."""

    def gate(index: LoadedIndex) -> list[str]:
        retriever = Retriever(
            index,
            encoder,
            catalog,
            query_instruction=query_instruction,
            top_k=top_k,
            threshold=threshold,
        )
        try:
            report = evaluate(retriever, catalog, eval_dir)
        except (OSError, KeyError, TypeError, yaml.YAMLError) as e:
            return [f"retrieval eval: query sets unusable in {eval_dir} ({e}); set MF_FAQ_EVAL_DIR"]
        return [f"retrieval eval: {f}" for f in report.failures]

    return gate


def format_report(report: RetrievalReport) -> str:
    k = f"hit@{report.top_k}"
    lines = [f"Retrieval eval (top_k={report.top_k}, τ={report.threshold})", ""]
    lines.append(f"{'set':18} {'n':>4} {'hit@1':>6} {k:>6} {'mrr':>6} {'false NF':>9}")
    for name in IN_CORPUS_SETS:
        if name in report.sets:
            s = report.sets[name]
            m = s.metrics
            lines.append(
                f"{name:18} {s.n:4} {m['hit@1']:6.3f} {m[k]:6.3f} {m['mrr']:6.3f} "
                f"{m['false_not_found']:9}"
            )
    lines += ["", f"{'not-found set':18} {'n':>4} {'stopped':>8}  by layer"]
    for name in (*OUT_OF_CORPUS_SETS, *REPORT_ONLY_SETS):
        if name in report.sets:
            s = report.sets[name]
            layers = ", ".join(f"{k2} {v}" for k2, v in sorted(s.metrics["by_layer"].items()))
            note = "  (report only)" if name in REPORT_ONLY_SETS else ""
            lines.append(f"{name:18} {s.n:4} {s.metrics['stopped']:8.3f}  {layers}{note}")
    ms = report.multi_scheme
    lines += ["", f"multi-scheme detected: {ms['detected']}/{ms['n']}"]
    if report.cosine:
        inc = report.cosine.get("in_corpus", {})
        ooc = report.cosine.get("out_of_corpus_retrieved", {})
        tau_range = report.cosine.get("tau_range")
        lines.append(
            f"top cosine: in-corpus min {inc.get('min')}, p5 {inc.get('p5')}, median "
            f"{inc.get('median')}; out-of-corpus that reached retrieval: max "
            f"{ooc.get('max', 'n/a')}"
        )
        if tau_range:
            inside = tau_range[0] <= report.threshold <= tau_range[1]
            lines.append(
                f"τ range (P3.6): {tau_range[0]}-{tau_range[1]}; τ={report.threshold} "
                + (
                    "is inside it"
                    if inside
                    else "is OUTSIDE it: re-derive MF_FAQ_RETRIEVAL_THRESHOLD"
                )
            )
    lat = report.latency_ms
    lines.append(f"latency: p50 {lat['p50']} ms, p95 {lat['p95']} ms, max {lat['max']} ms")
    misses = [m for s in report.sets.values() for m in s.misses if s.name not in REPORT_ONLY_SETS]
    if misses:
        lines += ["", "misses:"]
        for m in misses:
            why = m["reason"] or m["status"]
            lines.append(f"  {m['id']:10} {why:18} {m['q']}  -> {m['top'][:2]}")
    lines += ["", "PASS" if report.ok else "FAIL: " + "; ".join(report.failures)]
    return "\n".join(lines)
