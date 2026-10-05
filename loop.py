"""Memory helpers shared by app.py and the CLI.

Two memory modes, picked once from MEMORY_MODE:

- cloud: cognee.serve() points the SDK at Cognee Cloud. Every remember/recall
  call goes to the tenant at COGNEE_BASE_URL.
- local: the SDK runs on this machine (SQLite + LanceDB + Kuzu under .venv).
  Needs LLM_API_KEY. The local graph can be uploaded with cognee.push().
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import cognee
from cognee import SearchType
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
CODE_DATASET = os.getenv("CODE_DATASET") or "desk_code"
DEFAULT_CODE_REPO_URL = "https://github.com/scalekit-developers/cognee-scalekit-example"

USERS = {
    "alice": {
        "note": FIXTURES / "alice.txt",
        "summary": "Pro subscriber. Currently reading Avengers: Doomsday #51.",
        "question": "I want Avengers Doomsday comic edition #53",
        "expect": ("51", "53"),
        "questions": (
            {
                "id": "issue-53",
                "text": "I want Avengers Doomsday comic edition #53",
                "expect": ("51", "53"),
            },
            {
                "id": "pro",
                "text": "Am I a Pro subscriber?",
                "expect": ("pro",),
            },
        ),
    },
    "bob": {
        "note": FIXTURES / "bob.txt",
        "summary": "Not Pro. Last book payment did not go through.",
        "question": "I want to order the book again",
        "expect": ("payment", "order"),
        "questions": (
            {
                "id": "reorder",
                "text": "I want to order the book again",
                "expect": ("payment", "order"),
            },
            {
                "id": "payment",
                "text": "Did my last payment go through?",
                "expect": ("payment",),
            },
        ),
    },
}
RECALL_QUESTION = USERS["alice"]["question"]

# Fixed, deterministic code-graph questions about this repo. None of them calls
# an LLM: SearchType.CODE walks the graph enola built.
CODE_QUESTIONS = (
    {
        "id": "architecture",
        "text": "Draw the module map of this app",
        "code_query": {"operation": "architecture", "max_nodes": 40},
    },
    {
        "id": "impact",
        "text": "What breaks if I change require_customer()?",
        "code_query": {
            "operation": "impact_analysis",
            "target": "require_customer",
            "max_depth": 3,
            "diagram": "mermaid",
        },
    },
    {
        "id": "path",
        "text": "How does /api/ask reach Cognee recall?",
        "code_query": {
            "operation": "find_path",
            "from": "api_ask",
            "to": "recall_user",
            "diagram": "mermaid",
        },
    },
    {
        "id": "insights",
        "text": "What did the code analyzers flag?",
        "code_query": {"operation": "insights", "min_confidence": 0.5, "limit": 10},
    },
    {
        "id": "facts",
        "text": "List the first indexed modules and symbols",
        "code_query": {
            "operation": "query_facts",
            "kinds": ["module", "symbol", "route"],
            "limit": 20,
        },
    },
)


# --------------------------------------------------------------------------- mode


def memory_mode() -> str:
    """'cloud' or 'local'. Explicit MEMORY_MODE wins; else cloud when a tenant is set."""
    explicit = (os.getenv("MEMORY_MODE") or "").strip().lower()
    if explicit in ("cloud", "local"):
        return explicit
    return "cloud" if cloud_configured() else "local"


def cloud_configured() -> bool:
    return bool(os.getenv("COGNEE_API_KEY")) and bool(os.getenv("COGNEE_BASE_URL"))


def check_mode_env(mode: str) -> list[str]:
    """Names of env vars that are missing for the given mode."""
    if mode == "cloud":
        return [name for name in ("COGNEE_API_KEY", "COGNEE_BASE_URL") if not os.getenv(name)]
    return [name for name in ("LLM_API_KEY",) if not os.getenv(name)]


async def connect(mode: str) -> None:
    """Point the SDK at Cognee Cloud, or leave it local. Call once per process."""
    if mode == "cloud":
        await cognee.serve(
            url=os.environ["COGNEE_BASE_URL"],
            api_key=os.environ["COGNEE_API_KEY"],
        )


async def shutdown(mode: str) -> None:
    if mode == "cloud":
        await cognee.disconnect()
    else:
        # Local remember() may still be bridging improve() in the background.
        await cognee.wait_for_background_tasks()


def code_repo_source(mode: str) -> str:
    """What to index: the live checkout locally, the git URL on Cloud.

    Cognee Cloud clones the URL itself; a local path only means something to
    the machine that runs the SDK.
    """
    override = os.getenv("CODE_REPO_URL")
    if override:
        return override
    return str(ROOT) if mode == "local" else DEFAULT_CODE_REPO_URL


# ------------------------------------------------------------------------- results


def _as_dict(item) -> dict:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return {"text": str(item)}


def short_recall(results) -> str:
    """The first answer's text. HYBRID_COMPLETION returns one LLM answer per dataset."""
    if not results:
        return ""
    first = _as_dict(results[0])
    text = first.get("text") or (first.get("raw") or {}).get("value")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return str(results[0])


def code_result(results) -> dict:
    """The CODE operation's result dict, plus its diagram source if it drew one."""
    for item in results or ():
        entry = _as_dict(item)
        if entry.get("source") not in (None, "code"):
            continue
        raw = entry.get("raw") or entry.get("structured") or {}
        if isinstance(raw, dict) and raw.get("operation"):
            diagram = raw.get("diagram") or {}
            return {
                "operation": raw.get("operation"),
                "summary": raw.get("summary"),
                "diagram": diagram.get("source") if isinstance(diagram, dict) else None,
                "diagram_format": diagram.get("format") if isinstance(diagram, dict) else None,
                "result": raw,
            }
    return {"operation": None, "summary": None, "diagram": None, "result": None}


def is_conflict(exc: BaseException) -> bool:
    """HTTP 409: a pipeline still holds this dataset's lock. Retry after a pause."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 409:
        return True
    text = str(exc)
    return "409" in text or "Conflict" in text


def user_spec(user: str) -> dict:
    spec = USERS.get(user)
    if spec is None:
        raise ValueError("user must be alice or bob")
    return spec


# -------------------------------------------------------------------------- memory


async def seed_user(user: str) -> dict:
    """Write the customer's note into their dataset and finish the graph."""
    spec = user_spec(user)
    note = spec["note"].read_text(encoding="utf-8").strip()
    result = await cognee.remember(note, dataset_name=user)
    # remember() already runs improve() (self_improvement=True). Cloud may hand
    # back before the graph is queryable, so one explicit improve() makes the
    # seed call block until it is. improve is watermark-gated: when nothing is
    # new it reports already_completed and costs no LLM calls.
    await cognee.improve(user)
    return {
        "status": getattr(result, "status", None),
        "pipeline_run_id": str(getattr(result, "pipeline_run_id", "") or "") or None,
    }


async def recall_user(user: str, question: str | None = None, attempts: int = 4):
    """Answer from this customer's dataset only, with backoff on 409."""
    spec = user_spec(user)
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return await cognee.recall(
                query_text=question or spec["question"],
                # Pinned so the first result is always the LLM answer, never a
                # raw chunk the router picked on a keyless server.
                query_type=SearchType.HYBRID_COMPLETION,
                datasets=[user],
            )
        except Exception as exc:
            if not is_conflict(exc) or attempt == attempts:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)


async def push_user(user: str) -> dict:
    """Upload the local graph for this dataset to Cognee Cloud (no LLM calls there)."""
    result = await cognee.push(
        user,
        url=os.environ["COGNEE_BASE_URL"],
        api_key=os.environ["COGNEE_API_KEY"],
        mode="preserve",
    )
    return {
        "status": result.status,
        "dataset": result.dataset_name,
        "target": result.target_dataset,
        "nodes": result.num_nodes,
        "edges": result.num_edges,
        "pipeline_run_id": result.pipeline_run_id,
    }


# ---------------------------------------------------------------------- code graph


async def index_code(mode: str) -> dict:
    """Build the code graph of this repo. Deterministic; no LLM calls."""
    source = code_repo_source(mode)
    result = await cognee.remember(source, content_type="code", dataset_name=CODE_DATASET)
    items = getattr(result, "items", None) or []
    return {
        "status": getattr(result, "status", None),
        "source": source,
        "dataset": CODE_DATASET,
        "items": [
            {k: v for k, v in item.items() if k in ("source", "status", "error", "nodes", "edges")}
            for item in items
            if isinstance(item, dict)
        ],
    }


async def ask_code(question_id: str) -> dict:
    picked = next((q for q in CODE_QUESTIONS if q["id"] == question_id), None)
    if picked is None:
        raise ValueError("unknown code question")
    results = await cognee.recall(
        query_text="",
        scope=["code"],
        code_query=picked["code_query"],
        datasets=[CODE_DATASET],
    )
    out = code_result(results)
    out["question"] = picked["text"]
    out["code_query"] = picked["code_query"]
    return out


# ------------------------------------------------------------------------------ CLI


async def run(user: str, push: bool) -> None:
    mode = memory_mode()
    missing = check_mode_env(mode)
    if missing:
        print(f"missing env for {mode} mode: " + ", ".join(missing))
        sys.exit(1)
    if push and not cloud_configured():
        print("--push needs COGNEE_API_KEY and COGNEE_BASE_URL")
        sys.exit(1)

    await connect(mode)
    print(f"memory mode: {mode}")
    try:
        await seed_user(user)
        print(f"remembered + improved: {user}")
        results = await recall_user(user)
        print(f"recall: {short_recall(results)}")
        if push:
            if mode == "cloud":
                print("already in cloud mode; nothing to push")
            else:
                pushed = await push_user(user)
                print(f"pushed: {pushed}")
    finally:
        await shutdown(mode)


async def run_code(question_id: str | None) -> None:
    mode = memory_mode()
    missing = check_mode_env(mode) if mode == "cloud" else []
    if missing:
        print(f"missing env for {mode} mode: " + ", ".join(missing))
        sys.exit(1)
    await connect(mode)
    print(f"memory mode: {mode}")
    try:
        if question_id is None:
            print(await index_code(mode))
            return
        answer = await ask_code(question_id)
        if answer.get("summary"):
            print(answer["summary"])
        if answer.get("diagram"):
            print("```mermaid")
            print(answer["diagram"])
            print("```")
        elif answer.get("result") is not None:
            import json

            print(json.dumps(answer["result"], indent=2, default=str)[:4000])
    finally:
        await shutdown(mode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="alice")
    parser.add_argument(
        "--push", action="store_true", help="after recall, push the local graph to Cloud"
    )
    parser.add_argument(
        "--index-code", action="store_true", help="build the code graph of this repo"
    )
    parser.add_argument(
        "--code",
        choices=[q["id"] for q in CODE_QUESTIONS],
        help="run one fixed code-graph question",
    )
    args = parser.parse_args()
    if args.index_code:
        asyncio.run(run_code(None))
    elif args.code:
        asyncio.run(run_code(args.code))
    else:
        asyncio.run(run(args.user, args.push))


if __name__ == "__main__":
    main()
