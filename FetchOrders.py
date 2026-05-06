"""
Fetch Orders — Zerodha Kite Connect
=====================================
Fetches and displays today's order list from Zerodha.

Usage:
    python FetchOrders.py
    python FetchOrders.py --token YOUR_ACCESS_TOKEN
"""

import argparse
import json
import os
from datetime import datetime
from kiteconnect import KiteConnect
from get_access_token import API_KEY

TOKEN_FILE     = "token.json"
FALLBACK_TOKEN = "XYD3jx9fXoxgrm4qr2sqmcQnDgESGaSi"

STATUS_COLORS = {
    "COMPLETE":  "\033[92m",   # green
    "OPEN":      "\033[94m",   # blue
    "CANCELLED": "\033[93m",   # yellow
    "REJECTED":  "\033[91m",   # red
    "TRIGGER PENDING": "\033[96m",  # cyan
}
RESET = "\033[0m"
BOLD  = "\033[1m"


# ── Auth ──────────────────────────────────────────────────────────────────────

def resolve_token(cli_token: str = None) -> str:
    if cli_token:
        return cli_token
    try:
        with open(TOKEN_FILE) as f:
            token = json.load(f).get("access_token")
        if token:
            return token
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    print(f"No token found in {TOKEN_FILE}.")
    pasted = input("Paste your Kite access token: ").strip()
    if pasted:
        return pasted
    print(f"Using fallback token.")
    return FALLBACK_TOKEN


def get_kite(token: str) -> KiteConnect:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token)
    return kite


# ── Display ───────────────────────────────────────────────────────────────────

def fmt_time(ts) -> str:
    if not ts:
        return "—"
    try:
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return ts.strftime("%H:%M:%S")
    except Exception:
        return str(ts)


def color_status(status: str) -> str:
    c = STATUS_COLORS.get(status.upper(), "")
    return f"{c}{status}{RESET}" if c else status


def print_orders(orders: list):
    if not orders:
        print("\n  No orders found for today.\n")
        return

    col_w = [6, 14, 6, 8, 6, 10, 10, 10, 10, 18, 10]
    headers = ["#", "SYMBOL", "SIDE", "TYPE", "QTY", "PRICE", "AVG", "TRIGGER", "TIME", "STATUS", "ORDER ID"]

    sep = "─" * (sum(col_w) + len(col_w) * 3 + 1)
    fmt = "  " + " │ ".join(f"{{:<{w}}}" for w in col_w)

    print(f"\n{BOLD}  Today's Orders ({len(orders)}){RESET}\n  {sep}")
    print(fmt.format(*headers))
    print(f"  {sep}")

    for i, o in enumerate(orders, 1):
        side   = o.get("transaction_type", "")
        status = o.get("status", "")
        side_c = f"\033[92m{side}{RESET}" if side == "BUY" else f"\033[91m{side}{RESET}"

        print(fmt.format(
            str(i),
            o.get("tradingsymbol", "")[:14],
            side_c,
            o.get("order_type", ""),
            str(o.get("quantity", "")),
            str(o.get("price", 0) or "MKT"),
            str(o.get("average_price", 0) or "—"),
            str(o.get("trigger_price", 0) or "—"),
            fmt_time(o.get("order_timestamp")),
            color_status(status),
            o.get("order_id", ""),
        ))

    print(f"  {sep}\n")

    # Summary
    total    = len(orders)
    complete = sum(1 for o in orders if o.get("status") == "COMPLETE")
    open_    = sum(1 for o in orders if o.get("status") == "OPEN")
    rejected = sum(1 for o in orders if o.get("status") == "REJECTED")
    cancelled= sum(1 for o in orders if o.get("status") == "CANCELLED")

    print(f"  Total: {total}  │  "
          f"\033[92mComplete: {complete}{RESET}  │  "
          f"\033[94mOpen: {open_}{RESET}  │  "
          f"\033[91mRejected: {rejected}{RESET}  │  "
          f"\033[93mCancelled: {cancelled}{RESET}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch today's Kite orders")
    parser.add_argument("--token", help="Kite access token (overrides token.json)", default=None)
    args = parser.parse_args()

    print("\n========================================")
    print("   Zerodha Order Book — Kite API        ")
    print("========================================")

    token = resolve_token(args.token)
    kite  = get_kite(token)

    print("\nFetching orders...\n")
    try:
        orders = kite.orders()
    except Exception as e:
        print(f"  Error fetching orders: {e}\n")
        return

    print_orders(orders)


if __name__ == "__main__":
    main()
