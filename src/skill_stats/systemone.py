"""System One (decision-model) classification backend.

Per ADR-0004: instead of the rolling LLM session (ADR-0001), each commit is a
single POST to a System One API — a state string plus one Choice question over
the six verdicts (mirroring PROMPT_HEADER's rules) and, when Features exist,
one Choice question over the live Feature ids. The verdict drives the same
apply_reply path; the rationale column stores the returned confidence +
probability distribution (a decision model cannot produce prose), and
raw_llm_output keeps the full typed answer JSON.

Routes (all speak the same request/response shape):
- zen:        POST https://opencode.ai/zen/v1/systemone  (OPENCODE_API_KEY;
              served via the opencode Go/Zen subscription; model alias is the
              pinned `jev-1.13` — `jev-latest` is rejected upstream)
- openrouter: POST https://openrouter.ai/api/v1/systemone (OPENROUTER_API_KEY,
              openrouter.ai "System One API" surface; model `typesafe/jev-*`)
- typesafe:   POST https://api.typesafe.ai/v1/systemone  (TYPESAFE_API_KEY)

Exactly one route's key must be present unless
SKILL_STATS_SYSTEMONE_PROVIDER names one of them explicitly.
"""

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .classify import BODY_CAP, DIFF_CAP, Reply, feature_registry

ZEN_ENDPOINT = "https://opencode.ai/zen/v1/systemone"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/systemone"
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

# decision-model choice cannot cite a Feature unless one exists to cite
MAX_FEATURE_OPTIONS = 200

VERDICT_INSTRUCTIONS = (
    "Decide this git commit's single verdict from its TITLE, BODY and DIFF, "
    "judged beside the Features listed in the state. Pick the one option that "
    "best matches the commit's intent."
)

VERDICT_CRITERIA = {
    "new": "Introduces coherent new functionality and thereby defines a new Feature.",
    "existing": (
        "Continues or reworks the scope of a Feature already listed in the "
        "state's FEATURES block, without repairing a defect."
    ),
    "fix": (
        "Repairs or corrects lines of earlier work — the intent is repair, even "
        "when the message dresses it up; a fix of a fix still counts."
    ),
    "refactor": "Restructures code without behavior change and without defect repair.",
    "revert": "Undoes or reverses an earlier commit.",
    "cleanup": "Formatting, whitespace or other non-semantic change only.",
}

FEATURE_INSTRUCTIONS = (
    'The state\'s FEATURES block lists existing Feature ids. When the commit '
    'is "existing", which of those Feature ids does it continue?'
)


@dataclass(frozen=True)
class Route:
    """One System One vendor: endpoint + the exact model id to send."""

    vendor: str
    endpoint: str
    api_key: str


_ENV_KEYS = (
    ("typesafe", "TYPESAFE_API_KEY", TYPESAFE_ENDPOINT),
    ("openrouter", "OPENROUTER_API_KEY", OPENROUTER_ENDPOINT),
    ("zen", "OPENCODE_API_KEY", ZEN_ENDPOINT),
)


def resolve_route(env: dict[str, str] | None = None) -> Route:
    """Pick the System One route from the environment. One key must be set:
    with several, SKILL_STATS_SYSTEMONE_PROVIDER must name which vendor pays —
    silent key precedence would bill the wrong account."""
    env = env if env is not None else dict(os.environ)
    present = [(vendor, key, endpoint) for vendor, key, endpoint in _ENV_KEYS if env.get(key)]
    forced = env.get("SKILL_STATS_SYSTEMONE_PROVIDER")
    if forced is not None:
        by_vendor = {vendor: (vendor, key, endpoint) for vendor, key, endpoint in _ENV_KEYS}
        if forced not in by_vendor:
            raise ValueError(
                f"SKILL_STATS_SYSTEMONE_PROVIDER={forced!r} is not one of {sorted(by_vendor)}"
            )
        vendor, key, endpoint = by_vendor[forced]
        if not env.get(key):
            raise RuntimeError(f"SKILL_STATS_SYSTEMONE_PROVIDER={forced} but {key} is not set")
        return Route(vendor, endpoint, str(env[key]))
    if not present:
        names = ", ".join(key for _, key, _ in _ENV_KEYS)
        raise RuntimeError(f"no System One credentials in the environment (one of: {names})")
    if len(present) > 1:
        names = ", ".join(key for _, key, _ in present)
        raise RuntimeError(
            f"multiple System One credentials present ({names}); set"
            " SKILL_STATS_SYSTEMONE_PROVIDER to pick one"
        )
    vendor, key, endpoint = present[0]
    return Route(vendor, endpoint, str(env[key]))


def normalize_model(model: str, vendor: str) -> str:
    """Coerce the stored model id to what each vendor accepts. Zen serves only
    the pinned bare alias (verified live); openrouter wants its `typesafe/`
    namespace (a namespaced id passes through untouched); typesafe direct
    takes either native spelling."""
    if vendor == "zen":
        short = model.split("/")[-1]
        return "jev-1.13" if short in ("jev-latest", "jev-1.13.0") else short
    if vendor == "openrouter":
        return model if "/" in model else f"typesafe/{model}"
    return model


def http_poster(
    route: Route, timeout: float = 60.0, max_attempts: int = 4
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """POST a System One request; exponential backoff on rate-limit/overload
    (429/529/5xx) and a hard error message on other statuses."""

    def post(payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode()
        last: RuntimeError | None = None
        for attempt in range(max_attempts):
            req = urllib.request.Request(
                route.endpoint,
                data=data,
                headers={
                    "Authorization": f"Bearer {route.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "skill-stats-systemone/0.1",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    decoded: dict[str, Any] = json.loads(resp.read())
                    return decoded
            except urllib.error.HTTPError as exc:
                body = exc.read()[:2000].decode(errors="replace")
                last = RuntimeError(f"systemone HTTP {exc.code}: {body}")
                if exc.code in (429, 529, 500, 502, 503) and attempt < max_attempts - 1:
                    time.sleep(2**attempt)
                    continue
                raise last from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = RuntimeError(f"systemone request failed: {exc}")
                if attempt < max_attempts - 1:
                    time.sleep(2**attempt)
                    continue
                raise last from None
        raise last if last is not None else RuntimeError("systemone: no attempts made")

    return post


class SystemOneBackend:
    """One decision-model call per commit. Same classify() protocol as the
    opencode rolling-session backend; no session to resume."""

    def __init__(self, model: str, route: Route | None = None) -> None:
        self.model = model
        self.route = route if route is not None else resolve_route()
        self.normalized_model = normalize_model(model, self.route.vendor)
        self._poster: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def session_id(self) -> None:  # opencode backend parity; nothing to persist
        return None

    @property
    def poster(self) -> Callable[[dict[str, Any]], dict[str, Any]]:
        if self._poster is None:
            self._poster = http_poster(self.route)
        return self._poster

    def questions_for(self, feature_ids: list[str]) -> dict[str, Any]:
        questions: dict[str, Any] = {
            "verdict": {
                "type": "choice",
                "instructions": VERDICT_INSTRUCTIONS,
                "criteria": VERDICT_CRITERIA,
            }
        }
        if feature_ids:
            questions["feature"] = {
                "type": "choice",
                "instructions": FEATURE_INSTRUCTIONS,
                "criteria": {fid: None for fid in feature_ids},
            }
        return questions

    def classify(self, conn: sqlite3.Connection, walk_index: int) -> tuple[Reply, str]:
        rows = conn.execute(
            "SELECT id FROM features ORDER BY rowid LIMIT ?", (MAX_FEATURE_OPTIONS + 1,)
        ).fetchall()
        feature_ids = [str(r[0]) for r in rows[:MAX_FEATURE_OPTIONS]]
        over_cap = len(rows) > MAX_FEATURE_OPTIONS
        payload = {
            "state": state_text(conn, walk_index, over_cap),
            "model": self.normalized_model,
            "questions": self.questions_for(feature_ids),
        }
        data = self.poster(payload)
        raw = json.dumps(data)
        answers = data.get("answers", {})
        if not isinstance(answers, dict):
            return Reply("unknown", {}), raw
        verdict = answers.get("verdict", {}).get("choice")
        if verdict not in VERDICT_CRITERIA:
            return Reply("unknown", {}), raw
        why = why_from(answers.get("verdict", {}))
        if verdict == "existing":
            if not feature_ids:
                return Reply("unknown", {}), raw
            fid = answers.get("feature", {}).get("choice")
            if fid not in feature_ids:
                return Reply("unknown", {}), raw
            return Reply("existing", {"k": "existing", "why": why, "f": fid}), raw
        return Reply(verdict, {"k": verdict, "why": why}), raw


def state_text(conn: sqlite3.Connection, walk_index: int, truncated_features: bool = False) -> str:
    """The System One state: same caps as the LLM prompt (DIFF_CAP/BODY_CAP),
    plus the compact Feature registry. JSON — Jev reads text only."""
    commit = conn.execute("SELECT * FROM commits WHERE walk_index = ?", (walk_index,)).fetchone()
    if commit is None:
        raise ValueError(f"unknown walk_index {walk_index}")
    diff = commit["diff"] or ""
    if len(diff) > DIFF_CAP:
        diff = diff[:DIFF_CAP] + f"\n... [diff truncated, {len(commit['diff'])} chars total]"
    body = commit["message"] or ""
    if len(body) > BODY_CAP:
        body = body[:BODY_CAP] + " ... [body truncated]"
    merged_note = " (merge commit)" if commit["kind"] == "merge" else ""
    registry = feature_registry(conn)
    if truncated_features:
        registry += f"\n... [registry truncated to first {MAX_FEATURE_OPTIONS} features]"
    return (
        f"COMMIT {commit['sha'][:12]} walk={walk_index}"
        f" {commit['committed_at']}{merged_note}\n"
        f"TITLE: {commit['title']}\n"
        f"BODY:\n{body}\n"
        f"DIFF:\n{diff}\n"
        f"\nFEATURES (id: title — about [commits, sha]):\n{registry}"
    )


def why_from(answer: dict[str, Any], cap: int = 240) -> str:
    """The audit trail replaces prose with the typed numbers themselves."""
    parts = []
    if answer.get("confidence") is not None:
        parts.append(f"confidence={answer['confidence']}")
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict):
        parts.append("p=" + ",".join(f"{k}:{v:g}" for k, v in probabilities.items()))
    return (" ".join(parts)[:cap]) or ""
