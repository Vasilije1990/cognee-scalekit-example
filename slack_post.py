import argparse
import os
import sys

from dotenv import load_dotenv
from scalekit import ScalekitClient

load_dotenv()

REQUIRED_ENV = (
    "SCALEKIT_ENVIRONMENT_URL",
    "SCALEKIT_CLIENT_ID",
    "SCALEKIT_CLIENT_SECRET",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--text", required=True)
    args = parser.parse_args()

    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        print("missing env: " + ", ".join(missing))
        sys.exit(1)

    connection_name = os.getenv("SLACK_CONNECTION_NAME") or "slack"
    identifier = args.user

    sk_client = ScalekitClient(
        client_id=os.getenv("SCALEKIT_CLIENT_ID"),
        client_secret=os.getenv("SCALEKIT_CLIENT_SECRET"),
        env_url=os.getenv("SCALEKIT_ENVIRONMENT_URL"),
    )
    actions = sk_client.actions

    account = actions.get_or_create_connected_account(
        connection_name, identifier
    ).connected_account
    if account is None or account.status != "ACTIVE":
        link = actions.get_authorization_link(
            connection_name=connection_name,
            identifier=identifier,
        ).link
        print(link)
        sys.exit(0)

    response = actions.request(
        connection_name=connection_name,
        identifier=identifier,
        method="POST",
        path="/api/chat.postMessage",
        body={"channel": args.channel, "text": args.text},
    )

    try:
        payload = response.json()
    except ValueError:
        print(f"error: http {response.status_code}")
        sys.exit(1)

    if not isinstance(payload, dict):
        print(f"error: http {response.status_code}")
        sys.exit(1)

    ok = payload.get("ok")
    ts = payload.get("ts")
    error = payload.get("error")
    print(f"ok: {ok}")
    if ts is not None:
        print(f"ts: {ts}")
    if error:
        print(f"error: {error}")
    elif not ok:
        print(f"error: http {response.status_code}")
        sys.exit(1)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
