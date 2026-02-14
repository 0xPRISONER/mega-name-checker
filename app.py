#!/usr/bin/env python3
"""
MegaNames (.mega) Web Checker
Flask backend serving the multi-name availability checker.
"""

import re
import time
from flask import Flask, render_template, jsonify, request
from web3 import Web3
from eth_abi import decode as abi_decode, encode as abi_encode

# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

RPC_URL    = "https://mainnet.megaeth.com/rpc"
NAMES_ADDR = "0x5B424C6CCba77b32b9625a6fd5A30D409d20d997"
MC3_ADDR   = "0xcA11bde05977b3631167028862bE2a173976CA11"
BATCH_SIZE = 80

MEGA_NODE = bytes.fromhex(
    "892fab39f6d2ae901009febba7dbdd0fd85e8a1651be6b8901774cdef395852f"
)
GRACE_PERIOD = 90 * 86400

PRICING = {1: 1000, 2: 500, 3: 100, 4: 10}
DEFAULT_PRICE = 1

# ═══════════════════════════════════════════════════════════════
#  Flask App
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════
#  Web3 Setup (lazy init)
# ═══════════════════════════════════════════════════════════════

_w3 = None
_meganames = None
_multicall = None

MEGANAMES_ABI = [
    {
        "type": "function", "name": "records", "stateMutability": "view",
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
        "type": "function", "name": "ownerOf", "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]

MULTICALL3_ABI = [
    {
        "type": "function", "name": "aggregate3", "stateMutability": "payable",
        "inputs": [{
            "name": "calls", "type": "tuple[]",
            "components": [
                {"name": "target", "type": "address"},
                {"name": "allowFailure", "type": "bool"},
                {"name": "callData", "type": "bytes"},
            ],
        }],
        "outputs": [{
            "name": "returnData", "type": "tuple[]",
            "components": [
                {"name": "success", "type": "bool"},
                {"name": "returnData", "type": "bytes"},
            ],
        }],
    }
]


def get_w3():
    global _w3, _meganames, _multicall
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
        _meganames = _w3.eth.contract(
            address=Web3.to_checksum_address(NAMES_ADDR), abi=MEGANAMES_ABI
        )
        _multicall = _w3.eth.contract(
            address=Web3.to_checksum_address(MC3_ADDR), abi=MULTICALL3_ABI
        )
    return _w3, _meganames, _multicall


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

def price_usd(length):
    return PRICING.get(length, DEFAULT_PRICE)


def validate_label(raw):
    name = raw.strip().lower()
    if name.endswith(".mega"):
        name = name[:-5]
    name = name.strip()
    if not name:
        return None, "Empty name"
    if len(name) > 255:
        return None, "Too long (max 255 chars)"
    if not re.match(r"^[a-z0-9-]+$", name):
        return None, "Invalid characters (only a-z, 0-9, hyphen)"
    if name[0] == "-" or name[-1] == "-":
        return None, "Cannot start or end with hyphen"
    return name, None


def compute_token_id(label):
    label_hash = Web3.keccak(label.encode("utf-8"))
    return int.from_bytes(Web3.keccak(MEGA_NODE + label_hash), "big")


def encode_call(sig, types, values):
    selector = Web3.keccak(text=sig)[:4]
    return selector + abi_encode(types, values)


# ═══════════════════════════════════════════════════════════════
#  Batch Checker
# ═══════════════════════════════════════════════════════════════

def check_names(labels):
    w3, meganames, multicall = get_w3()
    now = int(time.time())
    target = Web3.to_checksum_address(NAMES_ADDR)

    all_results = []

    for batch_start in range(0, len(labels), BATCH_SIZE):
        batch = labels[batch_start:batch_start + BATCH_SIZE]

        calls = []
        token_ids = []

        for label in batch:
            tid = compute_token_id(label)
            token_ids.append(tid)
            calls.append((target, True, encode_call("records(uint256)", ["uint256"], [tid])))
            calls.append((target, True, encode_call("ownerOf(uint256)", ["uint256"], [tid])))

        try:
            raw = multicall.functions.aggregate3(calls).call()
        except Exception as e:
            # Fallback to individual calls
            for label in batch:
                all_results.append(_check_single(w3, meganames, label, now))
            continue

        for i, label in enumerate(batch):
            rec_ok, rec_data = raw[i * 2]
            own_ok, own_data = raw[i * 2 + 1]

            info = {
                "name": label,
                "display": f"{label}.mega",
                "available": True,
                "status": "available",
                "owner": None,
                "expires": 0,
                "expires_date": None,
                "price": price_usd(len(label)),
                "length": len(label),
            }

            if rec_ok and len(rec_data) >= 160:
                try:
                    stored_label, parent, expires_at, epoch, p_epoch = abi_decode(
                        ["string", "uint256", "uint64", "uint64", "uint64"], rec_data
                    )
                    if stored_label:
                        info["expires"] = expires_at
                        info["expires_date"] = time.strftime("%Y-%m-%d", time.gmtime(expires_at))
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

            if own_ok and len(own_data) >= 32:
                try:
                    (addr,) = abi_decode(["address"], own_data)
                    if addr != "0x" + "0" * 40:
                        info["owner"] = addr
                except Exception:
                    pass

            all_results.append(info)

    return all_results


def _check_single(w3, meganames, label, now):
    tid = compute_token_id(label)
    info = {
        "name": label,
        "display": f"{label}.mega",
        "available": True,
        "status": "available",
        "owner": None,
        "expires": 0,
        "expires_date": None,
        "price": price_usd(len(label)),
        "length": len(label),
    }
    try:
        rec = meganames.functions.records(tid).call()
        stored_label, parent, expires_at, epoch, p_epoch = rec
        if stored_label:
            info["expires"] = expires_at
            info["expires_date"] = time.strftime("%Y-%m-%d", time.gmtime(expires_at))
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


# ═══════════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json()
    if not data or "names" not in data:
        return jsonify({"error": "Missing 'names' field"}), 400

    raw_names = data["names"]
    if isinstance(raw_names, str):
        raw_names = [n for n in re.split(r"[,\s]+", raw_names.strip()) if n]

    if not raw_names:
        return jsonify({"error": "No names provided"}), 400

    if len(raw_names) > 500:
        return jsonify({"error": "Maximum 500 names per request"}), 400

    # Validate & deduplicate
    valid = []
    invalid = []
    seen = set()

    for raw in raw_names:
        label, err = validate_label(raw)
        if err:
            display = raw.strip().lower()
            if display.endswith(".mega"):
                display = display[:-5]
            invalid.append({
                "name": display or raw,
                "display": f"{display or raw}.mega",
                "available": False,
                "status": "invalid",
                "error": err,
                "owner": None,
                "expires": 0,
                "expires_date": None,
                "price": 0,
                "length": 0,
            })
        elif label not in seen:
            seen.add(label)
            valid.append(label)

    # Check valid names on-chain
    start = time.time()
    results = check_names(valid) if valid else []
    elapsed = round(time.time() - start, 2)

    all_results = invalid + results

    # Summary stats
    available_count = sum(1 for r in all_results if r.get("available") and r.get("status") != "invalid")
    taken_count = sum(1 for r in all_results if not r.get("available") and r.get("status") not in ("invalid",))
    invalid_count = len(invalid)
    total_cost = sum(r["price"] for r in all_results if r.get("available"))

    return jsonify({
        "results": all_results,
        "summary": {
            "total": len(all_results),
            "available": available_count,
            "taken": taken_count,
            "invalid": invalid_count,
            "total_cost_year": total_cost,
            "elapsed_seconds": elapsed,
        },
    })


@app.route("/api/health")
def health():
    try:
        w3, _, _ = get_w3()
        connected = w3.is_connected()
        block = w3.eth.block_number if connected else 0
        return jsonify({"status": "ok", "connected": connected, "block": block})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  Run
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 3000))
    print(f"\n  MegaNames Checker running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
