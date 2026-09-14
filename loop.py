import argparse
import asyncio
import os
import sys
from pathlib import Path

import cognee
from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV = ("COGNEE_API_KEY", "COGNEE_BASE_URL")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
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


def short_recall(results):
    if not results:
        return ""
    first = results[0]
    if isinstance(first, dict):
        text = first.get("text") or (first.get("raw") or {}).get("value")
        if isinstance(text, str) and text.strip():
            return text.strip()
    text = getattr(first, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    return str(first)


def is_conflict(exc: BaseException) -> bool:
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


async def recall_user(user: str, question: str | None = None):
    spec = user_spec(user)
    return await cognee.recall(
        query_text=question or spec["question"],
        datasets=[user],
    )


async def run(user: str) -> None:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        print("missing env: " + ", ".join(missing))
        sys.exit(1)

    spec = user_spec(user)
    note_text = spec["note"].read_text(encoding="utf-8").strip()

    await cognee.serve(
        url=os.environ["COGNEE_BASE_URL"],
        api_key=os.environ["COGNEE_API_KEY"],
    )
    print(f"connected: {os.environ['COGNEE_BASE_URL']}")

    try:
        await cognee.remember(note_text, dataset_name=user)
        print(f"remembered: {user}")

        # remember can return before Cloud can recall. improve finishes the graph.
        await cognee.improve(user)
        print(f"improved: {user}")

        try:
            results = await recall_user(user)
        except Exception as exc:
            if not is_conflict(exc):
                raise
            results = await recall_user(user)
        print(f"recall: {short_recall(results)}")
    finally:
        await cognee.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="alice")
    args = parser.parse_args()
    asyncio.run(run(args.user))


if __name__ == "__main__":
    main()
