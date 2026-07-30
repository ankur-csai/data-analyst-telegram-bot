#!/usr/bin/env python3
"""Exercise agent.py directly, no Telegram/Render involved.

Usage:
    python test_agent.py "Which state has the highest maternal mortality rate ..."
    python test_agent.py --chat mychat "first message" "second message" "final question"
"""
import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

import agent

FAKE_LOG_URL = "https://raw.githubusercontent.com/example/example/main/logs/run.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("messages", nargs="+", help="one or more messages, sent in order to the same chat")
    ap.add_argument("--chat", default="test-chat")
    args = ap.parse_args()

    for i, text in enumerate(args.messages):
        print(f"\n=== turn {i + 1} ===")
        print(f"> {text}")
        reply, trace = agent.answer(args.chat, text, FAKE_LOG_URL)
        print(f"< {reply}")
        print("--- tool trace ---")
        print(json.dumps(trace, indent=2, default=str)[:4000])


if __name__ == "__main__":
    sys.exit(main())
