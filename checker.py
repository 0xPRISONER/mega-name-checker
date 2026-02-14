#!/usr/bin/env python3
"""
MegaNames (.mega) Multi-Name Availability Checker

Check multiple .mega domain names at once using Multicall3 on MegaETH.
Uses batch RPC calls for maximum speed.

Usage:
    python checker.py bread fluffy megaeth vitalik
    python checker.py -f names.txt
    python checker.py                    (interactive mode)
    echo "bread,fluffy" | python checker.py --stdin
"""

import sys
import os
import re
import time
import json
import argparse

from web3 import Web3
from eth_abi import decode as abi_decode, encode as abi_encode

# ═══════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════

RPC_URL    = "https://mainnet.megaeth.com/rpc"
NAMES_ADDR = "0x5B424C6CCba77b32b9625a6fd5A30D409d20d997"   # MegaNames
MC3_ADDR   = "0xcA11bde05977b3631167028862bE2a173976CA11"   # Multicall3
BATCH_SIZE = 60   # names per multicall (120 calls: 2 per name)

# MEGA_NODE = keccak256(abi.encodePacked(bytes32(0), keccak256("mega")))
MEGA_NODE = bytes.fromhex(
    "892fab39f6d2ae901009febba7dbdd0fd85e8a1651be6b8901774cdef395852f"
)
GRACE_PERIOD = 90 * 86400  # 90 days in seconds

# Pricing: annual USD per character length
PRICING = {1: 1000, 2: 500, 3: 100, 4: 10}
DEFAULT_PRICE = 1  # $1/yr for 5+ chars

# ═══════════════════════════════════════════════════════════════════
#  Terminal Colors (auto-disabled when piped / NO_COLOR set)
# ═══════════════════════════════════════════════════════════════════

def _supports_color():
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()

if _supports_color():
    GRN, RED, YLW, CYN, MGN = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[95m"
    BLD, DIM, RST = "\033[1m", "\033[2m", "\033[0m"
else:
    GRN = RED = YLW = CYN = MGN = BLD = DIM = RST = ""

# ═══════════════════════════════════════════════════════════════════
#  ABI Definitions (minimal)
# ═══════════════════════════════════════════════════════════════════

MEGANAMES_ABI = [
    {
        "type": "function",
        "name": "records",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [
            {"name": "label", "type": "string"},
            {"name": "parent", "type": "uint256"},
            {"name": "expiresAt", "type": "uint64"},
            {"name": "epoch", "type": "uint64"},
            {"name": "parentEpoch", "type": "uint64"},
        ],
    },
    {
        "type": "function",
        "name": "ownerOf",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]

MULTICALL3_ABI = [
    {
        "type": "function",
        "name": "aggregate3",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            }
        ],
    }
]

# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════

def price_usd(length):
    """Annual price in USD for a name by character length."""
    return PRICING.get(length, DEFAULT_PRICE)


def validate_label(raw):
    """Validate and normalize a .mega label.
    Returns (normalized_label, error_string_or_None).
    """
    name = raw.strip().lower()
    if name.endswith(".mega"):
        name = name[:-5]
    name = name.strip()
    if not name:
        return None, "empty name"
    if len(name) > 255:
        return None, "too long (max 255 chars)"
    if not re.match(r"^[a-z0-9-]+$", name):
        return None, "invalid chars (only a-z, 0-9, hyphen)"
    if name[0] == "-" or name[-1] == "-":
        return None, "no leading/trailing hyphens"
    return name, None


def compute_token_id(label):
    """Compute tokenId = uint256(keccak256(MEGA_NODE || keccak256(label)))."""
    label_hash = Web3.keccak(label.encode("utf-8"))
    return int.from_bytes(Web3.keccak(MEGA_NODE + label_hash), "big")


def short_addr(addr):
    """0x1234...5678"""
    if not addr or addr == "0x" + "0" * 40:
        return ""
    return f"{addr[:6]}...{addr[-4:]}"


def ts_to_date(ts):
    """Unix timestamp → YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "—"


# ═══════════════════════════════════════════════════════════════════
#  Core Checker (Multicall3 batched)
# ═══════════════════════════════════════════════════════════════════

def _encode_call(selector_text, args_types, args_values):
    """Manually encode a contract call: selector + abi_encode(args)."""
    selector = Web3.keccak(text=selector_text)[:4]
    return selector + abi_encode(args_types, args_values)


def check_batch(w3, meganames, multicall, labels):
    """Check a batch of labels using Multicall3.

    For each label, 2 sub-calls are batched:
      1. records(tokenId)  – registration data
      2. ownerOf(tokenId)  – current owner (may fail if unregistered)

    Returns list of result dicts.
    """
    now = int(time.time())
    target = Web3.to_checksum_address(NAMES_ADDR)

    calls = []
    token_ids = []

    for label in labels:
        tid = compute_token_id(label)
        token_ids.append(tid)

        # records(tokenId)
        rec_cd = _encode_call("records(uint256)", ["uint256"], [tid])
        calls.append((target, True, rec_cd))

        # ownerOf(tokenId)
        own_cd = _encode_call("ownerOf(uint256)", ["uint256"], [tid])
        calls.append((target, True, own_cd))

    # Single RPC call for entire batch
    raw = multicall.functions.aggregate3(calls).call()

    results = []
    for i, label in enumerate(labels):
        rec_ok, rec_data = raw[i * 2]
        own_ok, own_data = raw[i * 2 + 1]

        info = {
            "name": label,
            "token_id": hex(token_ids[i]),
            "available": True,
            "status": "available",
            "owner": None,
            "expires": 0,
            "price": price_usd(len(label)),
        }

        # Parse records()
        if rec_ok and len(rec_data) >= 160:
            try:
                stored_label, parent, expires_at, epoch, p_epoch = abi_decode(
                    ["string", "uint256", "uint64", "uint64", "uint64"], rec_data
                )
                if stored_label:  # record exists
                    info["expires"] = expires_at
                    if now <= expires_at:
                        info["available"] = False
                        info["status"] = "taken"
                    elif now <= expires_at + GRACE_PERIOD:
                        info["available"] = False
                        info["status"] = "grace"
                    else:
                        info["status"] = "expired"  # re-registerable
            except Exception:
                pass

        # Parse ownerOf()
        if own_ok and len(own_data) >= 32:
            try:
                (addr,) = abi_decode(["address"], own_data)
                if addr != "0x" + "0" * 40:
                    info["owner"] = addr
            except Exception:
                pass

        results.append(info)

    return results


def check_single(w3, meganames, label):
    """Fallback: check one name with individual RPC calls."""
    now = int(time.time())
    tid = compute_token_id(label)
    info = {
        "name": label,
        "token_id": hex(tid),
        "available": True,
        "status": "available",
        "owner": None,
        "expires": 0,
        "price": price_usd(len(label)),
    }

    try:
        rec = meganames.functions.records(tid).call()
        stored_label, parent, expires_at, epoch, p_epoch = rec
        if stored_label:
            info["expires"] = expires_at
            if now <= expires_at:
                info["available"] = False
                info["status"] = "taken"
            elif now <= expires_at + GRACE_PERIOD:
                info["available"] = False
                info["status"] = "grace"
            else:
                info["status"] = "expired"
    except Exception:
        pass

    try:
        owner = meganames.functions.ownerOf(tid).call()
        if owner != "0x" + "0" * 40:
            info["owner"] = owner
    except Exception:
        pass

    return info


# ═══════════════════════════════════════════════════════════════════
#  Display
# ═══════════════════════════════════════════════════════════════════

def print_banner():
    print()
    print(f"  {BLD}{'═' * 58}{RST}")
    print(f"  {BLD}    MEGANAMES (.mega) BULK AVAILABILITY CHECKER{RST}")
    print(f"  {DIM}    meganame.market  ·  MegaETH  ·  Chain ID 4326{RST}")
    print(f"  {BLD}{'═' * 58}{RST}")
    print()


def print_result(r, pad):
    name_col = r["name"].ljust(pad)

    if r.get("invalid"):
        print(f"  {YLW}⚠{RST}  {name_col}  {YLW}INVALID{RST}       {DIM}{r['error']}{RST}")
        return

    if r["available"]:
        price = f"${r['price']}/yr"
        extra = f"  {DIM}(re-register){RST}" if r["status"] == "expired" else ""
        print(f"  {GRN}✓{RST}  {name_col}  {GRN}AVAILABLE{RST}     {CYN}{price}{RST}{extra}")
    else:
        if r["status"] == "grace":
            tag = f"{YLW}GRACE PERIOD{RST}"
        else:
            tag = f"{RED}TAKEN{RST}       "

        parts = []
        if r.get("owner"):
            parts.append(f"owner: {DIM}{short_addr(r['owner'])}{RST}")
        if r.get("expires") and r["expires"] > 0:
            parts.append(f"expires: {DIM}{ts_to_date(r['expires'])}{RST}")
        detail = "  ".join(parts)
        print(f"  {RED}✗{RST}  {name_col}  {tag}  {detail}")


def print_summary(results):
    avail   = [r for r in results if r.get("available") and not r.get("invalid")]
    taken   = [r for r in results if not r.get("available") and not r.get("invalid")]
    invalid = [r for r in results if r.get("invalid")]
    total   = len(results)

    print()
    print(f"  {'─' * 58}")
    parts = [f"Total: {BLD}{total}{RST}"]
    if avail:
        parts.append(f"{GRN}Available: {len(avail)}{RST}")
    if taken:
        parts.append(f"{RED}Taken: {len(taken)}{RST}")
    if invalid:
        parts.append(f"{YLW}Invalid: {len(invalid)}{RST}")
    print(f"  {' │ '.join(parts)}")

    if avail:
        cost = sum(r["price"] for r in avail)
        names_list = ", ".join(r["name"] + ".mega" for r in avail[:10])
        if len(avail) > 10:
            names_list += f" ...+{len(avail) - 10} more"
        print(f"  {GRN}Register all available: ~${cost}/yr{RST}")
        print(f"  {DIM}{names_list}{RST}")

    print(f"  {'─' * 58}")
    print()


# ═══════════════════════════════════════════════════════════════════
#  Input Collection
# ═══════════════════════════════════════════════════════════════════

def split_names(text):
    """Split a string of names by commas, spaces, or newlines."""
    return [n for n in re.split(r"[,\s]+", text.strip()) if n]


def collect_names(args):
    """Gather names from all input sources."""
    raw = []

    # From file
    if args.file:
        try:
            with open(args.file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        raw.extend(split_names(line))
        except FileNotFoundError:
            print(f"{RED}Error: File '{args.file}' not found{RST}", file=sys.stderr)
            sys.exit(1)

    # From stdin
    if args.stdin:
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                raw.extend(split_names(line))

    # From CLI arguments
    if args.names:
        for name in args.names:
            raw.extend(split_names(name))

    # Interactive mode
    if not raw:
        print_banner()
        print(f"  {DIM}Enter names to check (comma/space separated).{RST}")
        print(f"  {DIM}Press Enter on empty line when done, Ctrl+C to exit.{RST}")
        print()
        try:
            while True:
                try:
                    line = input(f"  {CYN}names >{RST} ")
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    if raw:
                        break
                    continue
                raw.extend(split_names(line))
        except KeyboardInterrupt:
            print()
            sys.exit(0)

    if not raw:
        print(f"{YLW}No names provided. Use --help for usage info.{RST}", file=sys.stderr)
        sys.exit(1)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for n in raw:
        key = n.strip().lower()
        if key.endswith(".mega"):
            key = key[:-5]
        if key and key not in seen:
            seen.add(key)
            unique.append(n)

    return unique


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Check availability of .mega names on MegaETH",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python checker.py bread fluffy megaeth vitalik
  python checker.py -f names.txt
  python checker.py --stdin < names.txt
  echo "bread,fluffy,vitalik" | python checker.py --stdin
  python checker.py --json bread fluffy > results.json
        """,
    )
    parser.add_argument("names", nargs="*", help="Names to check (without .mega)")
    parser.add_argument("-f", "--file", help="File with names (one per line)")
    parser.add_argument("--stdin", action="store_true", help="Read names from stdin")
    parser.add_argument("--rpc", default=RPC_URL, help="MegaETH RPC endpoint")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Batch size")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    # Disable colors for JSON output
    if args.json:
        global GRN, RED, YLW, CYN, MGN, BLD, DIM, RST
        GRN = RED = YLW = CYN = MGN = BLD = DIM = RST = ""

    # ── Collect & validate names ──────────────────────────────
    raw_names = collect_names(args)

    valid_labels = []
    invalid_results = []

    for raw in raw_names:
        label, err = validate_label(raw)
        if err:
            display = raw.strip().lower()
            if display.endswith(".mega"):
                display = display[:-5]
            invalid_results.append({
                "name": display or raw,
                "invalid": True,
                "error": err,
                "available": False,
            })
        else:
            valid_labels.append(label)

    total_count = len(valid_labels) + len(invalid_results)

    if not args.json:
        if not (args.file or args.stdin or args.names):
            pass  # Banner already printed in interactive mode
        else:
            print_banner()
        print(f"  Checking {BLD}{total_count}{RST} name{'s' if total_count != 1 else ''} on MegaETH...")
        print()

    # ── Connect to MegaETH ────────────────────────────────────
    try:
        w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
        if not w3.is_connected():
            print(f"{RED}Error: Cannot connect to MegaETH RPC{RST}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"{RED}Error: {e}{RST}", file=sys.stderr)
        sys.exit(1)

    meganames = w3.eth.contract(
        address=Web3.to_checksum_address(NAMES_ADDR),
        abi=MEGANAMES_ABI,
    )
    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(MC3_ADDR),
        abi=MULTICALL3_ABI,
    )

    # ── Check names in batches ────────────────────────────────
    checked = []
    bs = args.batch_size

    for i in range(0, len(valid_labels), bs):
        batch = valid_labels[i : i + bs]

        if not args.json and len(valid_labels) > bs:
            end = min(i + bs, len(valid_labels))
            print(f"  {DIM}[batch {i+1}-{end} of {len(valid_labels)}]{RST}")

        try:
            batch_results = check_batch(w3, meganames, multicall, batch)
            checked.extend(batch_results)
        except Exception as e:
            if not args.json:
                print(f"  {YLW}Multicall failed ({e}), falling back to individual calls...{RST}")
            for label in batch:
                try:
                    r = check_single(w3, meganames, label)
                    checked.append(r)
                except Exception as ex:
                    checked.append({
                        "name": label,
                        "available": None,
                        "status": "error",
                        "error": str(ex),
                        "owner": None,
                        "expires": 0,
                        "price": price_usd(len(label)),
                    })

    # ── Combine results ───────────────────────────────────────
    all_results = invalid_results + checked

    # ── Output ────────────────────────────────────────────────
    if args.json:
        output = []
        for r in all_results:
            item = {
                "name": r["name"] + ".mega",
                "available": r.get("available", False),
                "status": r.get("status", "invalid" if r.get("invalid") else "unknown"),
            }
            if r.get("owner"):
                item["owner"] = r["owner"]
            if r.get("expires") and r["expires"] > 0:
                item["expires_unix"] = r["expires"]
                item["expires_date"] = ts_to_date(r["expires"])
            if r.get("price") is not None:
                item["price_usd_year"] = r["price"]
            if r.get("token_id"):
                item["token_id"] = r["token_id"]
            if r.get("error"):
                item["error"] = r["error"]
            output.append(item)
        print(json.dumps(output, indent=2))
    else:
        pad = max((len(r["name"]) for r in all_results), default=10)
        pad = min(max(pad, 8), 30)

        for r in all_results:
            print_result(r, pad)

        print_summary(all_results)


if __name__ == "__main__":
    main()
