import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from anthropic import Anthropic
from pydantic import BaseModel

MODEL = "claude-sonnet-5"
SCRIPT_DIR = Path(__file__).parent
CAUSES = [
    "pilot ended",
    "champion left",
    "use case failed",
    "seasonal",
    "new team onboarded",
    "successful rollout",
    "unknown",
]


class SignalAnalysis(BaseModel):
    cause: Literal[
        "pilot ended",
        "champion left",
        "use case failed",
        "seasonal",
        "new team onboarded",
        "successful rollout",
        "unknown",
    ]
    confidence: float
    note: str



def load_usage(path):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            row["tokens"] = int(row["tokens"])
            row["seats"] = int(row["seats"])
            row["active_users"] = int(row["active_users"])
            rows.append(row)
    return rows


def monthly_totals(account_rows):
    months = sorted(set(r["month"] for r in account_rows))
    totals = {}
    for m in months:
        m_rows = [r for r in account_rows if r["month"] == m]
        totals[m] = {
            "tokens": sum(r["tokens"] for r in m_rows),
            "seats": sum(r["seats"] for r in m_rows),
            "departments": set(r["department"] for r in m_rows),
        }
    return months, totals


def detect_signals(account_name, account_rows):
    months, totals = monthly_totals(account_rows)
    signals = []
    seen_departments = set()
    decline_streak = 0

    for i, m in enumerate(months):
        depts_this_month = totals[m]["departments"]

        if i > 0:
            prev = totals[months[i - 1]]
            reasons = []

            new_depts = depts_this_month - seen_departments
            if new_depts:
                reasons.append(f"new department(s) appeared: {', '.join(sorted(new_depts))}")

            if prev["tokens"] > 0 and totals[m]["tokens"] > prev["tokens"] * 1.3:
                pct = (totals[m]["tokens"] / prev["tokens"] - 1) * 100
                reasons.append(f"total tokens rose {pct:.0f}% month over month")

            if totals[m]["seats"] > prev["seats"]:
                reasons.append(f"seats increased from {prev['seats']} to {totals[m]['seats']}")

            if reasons:
                signals.append({"account": account_name, "month": m, "type": "growth", "reasons": reasons})

            if totals[m]["tokens"] < prev["tokens"]:
                decline_streak += 1
            else:
                decline_streak = 0
            if decline_streak == 2:
                signals.append({
                    "account": account_name,
                    "month": m,
                    "type": "decline",
                    "reasons": ["total tokens fell for two consecutive months"],
                })

        seen_departments |= depts_this_month

    return signals


def format_history(account_rows):
    header = "month,department,tokens,seats,active_users"
    lines = [
        f"{r['month']},{r['department']},{r['tokens']},{r['seats']},{r['active_users']}"
        for r in sorted(account_rows, key=lambda r: (r["month"], r["department"]))
    ]
    return "\n".join([header] + lines)


def build_prompt(signal, account_rows):
    history = format_history(account_rows)
    reasons = "; ".join(signal["reasons"])

    if signal["type"] == "decline":
        task = (
            "Draft an outreach note from this customer's customer success manager to their "
            "main contact at the account. Invent a plausible first name for the contact and a "
            "different plausible first name for the customer success manager (yourself).\n"
            "Structure it exactly like this:\n"
            "- Open with \"Hi <contact first name>,\" on its own line.\n"
            "- Then exactly three sentences, in this order:\n"
            "  1. State the change you saw in this account's usage, citing at least one specific "
            "number from the history below.\n"
            "  2. Offer one specific, concrete form of help - for example a thirty-minute "
            "refresher for their team, or a review of their use case.\n"
            "  3. Propose a specific day and time to talk.\n"
            "- Close with a warm sign-off (e.g. \"Best,\") followed by the customer success "
            "manager's first name on the next line.\n"
            "Never use the phrases \"I noticed\", \"check in\", or \"full value from the "
            "platform\", or close paraphrases of them."
        )
    else:
        task = (
            "Draft an internal note from the account team's analyst to the account team, "
            "about this account. Write exactly three sentences, in this order:\n"
            "  1. State the change you saw in this account's usage, citing at least one specific "
            "number from the history below.\n"
            "  2. Name the specific next department at this account to approach.\n"
            "  3. State the proposed ask.\n"
            "If you reference the account's payoff, call it \"proof of adoption\" - never "
            "\"proof of ROI\"."
        )

    return (
        f"Account: {signal['account']}\n"
        f"Signal detected in {signal['month']}: {signal['type']} "
        f"({reasons}).\n\n"
        f"Full monthly usage history for this account:\n{history}\n\n"
        f"First, classify the most likely cause of this signal. Choose exactly one value "
        f"from this fixed list: {', '.join(CAUSES)}.\n"
        f"Also give your confidence in that classification as a number between 0 and 1.\n\n"
        f"Second, {task}"
    )


def analyze_signal(client, signal, account_rows):
    prompt = build_prompt(signal, account_rows)
    response = client.messages.parse(
        model=MODEL,
        max_tokens=1024,
        system=(
            "You are a revenue operations analyst reviewing product usage signals for a "
            "B2B SaaS company. Be concise and concrete; ground your classification in the "
            "usage history provided."
        ),
        messages=[{"role": "user", "content": prompt}],
        output_format=SignalAnalysis,
    )
    return response.parsed_output


def order_signals(signals, mode):
    priority = {"decline": 0, "growth": 1} if mode == "success" else {"growth": 0, "decline": 1}
    return sorted(signals, key=lambda s: (priority[s["type"]], s["account"], s["month"]))


def print_table(rows):
    headers = ["account", "signal type", "cause", "confidence"]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))


def write_notes(ordered, analyses, mode, path):
    with open(path, "w") as f:
        f.write(f"# Signal Notes\n\nGenerated by monitor.py in `{mode}` mode.\n\n")
        for signal in ordered:
            analysis = analyses[id(signal)]
            f.write(f"## {signal['account']} — {signal['type']} signal ({signal['month']})\n\n")
            f.write(f"**Cause:** {analysis.cause} (confidence: {analysis.confidence:.0%})\n\n")
            f.write(f"{analysis.note}\n\n")


def main():
    parser = argparse.ArgumentParser(description="Detect account usage signals and draft notes with Claude.")
    parser.add_argument("--mode", choices=["success", "growth"], default="success",
                         help="success: show decline signals first. growth: show growth signals first.")
    parser.add_argument("--usage-file", default=str(SCRIPT_DIR / "usage.csv"))
    args = parser.parse_args()

    load_dotenv()
    client = Anthropic()

    rows = load_usage(args.usage_file)
    accounts = defaultdict(list)
    for row in rows:
        accounts[row["account"]].append(row)

    all_signals = []
    for account_name, account_rows in accounts.items():
        all_signals.extend(detect_signals(account_name, account_rows))

    ordered = order_signals(all_signals, args.mode)

    analyses = {}
    table_rows = []
    for signal in ordered:
        analysis = analyze_signal(client, signal, accounts[signal["account"]])
        analyses[id(signal)] = analysis
        table_rows.append([signal["account"], signal["type"], analysis.cause, f"{analysis.confidence:.0%}"])

    print(f"\nMode: {args.mode}\n")
    print_table(table_rows)

    write_notes(ordered, analyses, args.mode, SCRIPT_DIR / "signals.md")
    print(f"\nWrote {len(ordered)} note(s) to signals.md")


if __name__ == "__main__":
    main()
