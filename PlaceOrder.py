"""
Place Order — Zerodha Kite Connect
===================================
Interactive CLI to search a stock, view live price, and place a buy/sell order.

Usage:
    python PlaceOrder.py
"""

import json
import os
from kiteconnect import KiteConnect
from get_access_token import API_KEY

TOKEN_FILE     = "token.json"
FALLBACK_TOKEN = "XYD3jx9fXoxgrm4qr2sqmcQnDgESGaSi"
EXCHANGE       = "NSE"


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_kite() -> KiteConnect:
    token = None
    try:
        with open(TOKEN_FILE) as f:
            token = json.load(f).get("access_token")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if not token:
        print("No token found in token.json — using fallback token.")
        token = FALLBACK_TOKEN
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token)
    return kite


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_ltp(kite: KiteConnect, symbol: str) -> float:
    quote = kite.ltp(f"{EXCHANGE}:{symbol}")
    return quote[f"{EXCHANGE}:{symbol}"]["last_price"]


def get_instrument(kite: KiteConnect, symbol: str) -> dict:
    instruments = kite.instruments(EXCHANGE)
    for inst in instruments:
        if inst["tradingsymbol"] == symbol and inst["instrument_type"] == "EQ":
            return inst
    return None


def prompt(msg: str, default: str = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val if val else default


def choose(msg: str, options: list) -> str:
    print(f"\n{msg}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        val = input("Enter choice: ").strip()
        if val.isdigit() and 1 <= int(val) <= len(options):
            return options[int(val) - 1]
        print("  Invalid choice. Try again.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n========================================")
    print("   Zerodha Order Placement — Kite API   ")
    print("========================================\n")

    kite = get_kite()

    # 1. Stock symbol
    symbol = input("Enter stock symbol (e.g. RELIANCE): ").strip().upper()
    if not symbol:
        raise SystemExit("No symbol entered.")

    # 2. Validate & fetch live price
    print(f"\nFetching details for {symbol}...")
    inst = get_instrument(kite, symbol)
    if not inst:
        raise SystemExit(f"Instrument '{symbol}' not found on {EXCHANGE}.")

    ltp = get_ltp(kite, symbol)
    print(f"\n  Symbol      : {symbol}")
    print(f"  Name        : {inst['name']}")
    print(f"  Exchange    : {EXCHANGE}")
    print(f"  Lot size    : {inst['lot_size']}")
    print(f"  Last price  : ₹{ltp:.2f}")

    # 3. Transaction type
    transaction_type = choose("Transaction type:", ["BUY", "SELL"])

    # 4. Quantity
    while True:
        qty_str = input("\nQuantity (number of shares): ").strip()
        if qty_str.isdigit() and int(qty_str) > 0:
            quantity = int(qty_str)
            break
        print("  Invalid quantity. Enter a positive integer.")

    # 5. Product type
    product = choose("Product type:", [
        "CNC  — Delivery (equity)",
        "MIS  — Intraday",
        "NRML — Normal (F&O)",
    ])
    product_code = product.split()[0]

    # 6. Order type
    order_type = choose("Order type:", [
        "MARKET — Execute at current market price",
        "LIMIT  — Execute at a specific price",
        "SL     — Stop-loss limit order",
        "SL-M   — Stop-loss market order",
    ])
    order_type_code = order_type.split()[0]

    # 7. Price (if LIMIT or SL)
    price = 0
    trigger_price = 0

    if order_type_code in ("LIMIT", "SL"):
        while True:
            p = input(f"\nLimit price [LTP ₹{ltp:.2f}]: ").strip()
            try:
                price = float(p) if p else ltp
                break
            except ValueError:
                print("  Invalid price.")

    if order_type_code in ("SL", "SL-M"):
        while True:
            tp = input(f"\nTrigger price: ").strip()
            try:
                trigger_price = float(tp)
                break
            except ValueError:
                print("  Invalid trigger price.")

    # 8. Validity
    validity = choose("Order validity:", ["DAY", "IOC"])

    # 9. Confirm
    estimated = quantity * (price if price else ltp)
    print("\n─────────────────────────────────────")
    print("          ORDER SUMMARY")
    print("─────────────────────────────────────")
    print(f"  Symbol          : {EXCHANGE}:{symbol}")
    print(f"  Transaction     : {transaction_type}")
    print(f"  Quantity        : {quantity}")
    print(f"  Product         : {product_code}")
    print(f"  Order type      : {order_type_code}")
    print(f"  Price           : ₹{price:.2f}" if price else f"  Price           : MARKET (LTP ₹{ltp:.2f})")
    if trigger_price:
        print(f"  Trigger price   : ₹{trigger_price:.2f}")
    print(f"  Validity        : {validity}")
    print(f"  Est. value      : ₹{estimated:,.2f}")
    print("─────────────────────────────────────")

    confirm = input("\nConfirm and place order? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("\nOrder cancelled.")
        return

    # 10. Place order
    params = dict(
        exchange          = EXCHANGE,
        tradingsymbol     = symbol,
        transaction_type  = transaction_type,
        quantity          = quantity,
        product           = product_code,
        order_type        = order_type_code,
        validity          = validity,
        price             = price,
        trigger_price     = trigger_price,
    )

    try:
        order_id = kite.place_order(variety=kite.VARIETY_REGULAR, **params)
        print(f"\n✓ Order placed successfully!")
        print(f"  Order ID : {order_id}")
        print(f"\nTrack your order at: https://kite.zerodha.com/orders")
    except Exception as e:
        print(f"\n✗ Order failed: {e}")


if __name__ == "__main__":
    main()
