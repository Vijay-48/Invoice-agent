"""Check the Composio WhatsApp connection used for reminders (FOLLOWUP_WHATSAPP_VIA=composio, D-114).

    python -m scripts.composio_whatsapp                      lists the account's numbers and approved templates
    python -m scripts.composio_whatsapp --send +919876543210  sends one test message (needs FOLLOWUP_DRY_RUN=false)

The first shows the phone_number_id to put in COMPOSIO_WHATSAPP_PHONE_NUMBER_ID and the templates that can
go in COMPOSIO_WHATSAPP_TEMPLATE (body "{{1}}"). Reads only, unless --send is given."""
import argparse
import json

from app import outbound
from app.config import settings


def run(slug: str, args: dict) -> dict:
    res = outbound.composio_whatsapp().tools.execute(slug, args, user_id=settings.composio_whatsapp_user_id,
                                                     connected_account_id=settings.composio_whatsapp_account_id)
    if not res.get("successful"):
        raise SystemExit(f"{slug} failed: {res.get('error')}")
    return res.get("data") or {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", metavar="NUMBER", help="send one test reminder to this number (+91...)")
    a = parser.parse_args()
    if not (settings.composio_api_key and settings.composio_whatsapp_account_id):
        raise SystemExit("Set COMPOSIO_API_KEY and COMPOSIO_WHATSAPP_ACCOUNT_ID in .env first")
    if a.send:
        result = outbound.send_whatsapp_composio(a.send, "Test reminder from the invoice app.",
                                                 template=bool(settings.composio_whatsapp_template))
        print(f"{result.status}: {result.detail or result.sid or ''}")
        return
    print("Phone numbers (phone_number_id = the 'id'):")
    print(json.dumps(run("WHATSAPP_GET_PHONE_NUMBERS", {"limit": 20}), indent=2, ensure_ascii=False)[:3000])
    print("\nApproved templates:")
    print(json.dumps(run("WHATSAPP_GET_MESSAGE_TEMPLATES", {"status": "APPROVED", "limit": 25}),
                     indent=2, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
