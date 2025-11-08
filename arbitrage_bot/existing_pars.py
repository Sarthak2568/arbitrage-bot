#!/usr/bin/env python3
import os
import sys
import json
import time
from pathlib import Path

from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from requests.exceptions import HTTPError

# ——— CONFIGURATION ———
RPC_URL = os.getenv(
    "WEB3_PROVIDER_URI",
    "https://mainnet.infura.io/v3/6f29b02833ab4700bc541dc4e436a35f"
)
if not RPC_URL:
    print("ERROR: WEB3_PROVIDER_URI not set.")
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    print(f"ERROR: Cannot connect to RPC at {RPC_URL}")
    sys.exit(1)

FACTORY_ADDRESS = w3.to_checksum_address("0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f")
ABIS_FILE       = Path(__file__).with_name("abis.txt")

# rate-limit / batching config
BATCH_SIZE               = 500     # how many pairs to fetch per loop
SLEEP_BETWEEN_BATCHES    = 1       # secs between each batch
RATE_LIMIT_SLEEP         = 60      # secs to sleep on 429
FULL_SCAN_INTERVAL       = 3600    # secs between full scans

# ——— FACTORY ABI ———
FACTORY_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "allPairsLength",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "allPairs",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# ——— PAIR & TOKEN ABIs ———
UNISWAP_PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    }
]

def load_seen_pairs() -> set:
    seen = set()
    if ABIS_FILE.exists():
        for line in ABIS_FILE.read_text().splitlines():
            try:
                rec = json.loads(line)
                addr = rec.get("pairAddress", "").lower()
                if addr:
                    seen.add(addr)
            except json.JSONDecodeError:
                continue
    return seen

def append_record(rec: dict):
    with ABIS_FILE.open("a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")

def fetch_all_pairs():
    factory = w3.eth.contract(address=FACTORY_ADDRESS, abi=FACTORY_ABI)

    # get total pairs
    while True:
        try:
            total = factory.functions.allPairsLength().call()
            break
        except (HTTPError, ContractLogicError) as e:
            print(f"[WARN] allPairsLength failed ({e}), sleeping {RATE_LIMIT_SLEEP}s")
            time.sleep(RATE_LIMIT_SLEEP)

    print(f"[INFO] Factory has {total} pairs")

    seen = load_seen_pairs()
    added = 0

    for start in range(0, total, BATCH_SIZE):
        end = min(start + BATCH_SIZE, total)
        print(f"[INFO] Fetching pairs {start}-{end-1}")

        for idx in range(start, end):
            # fetch pair address
            try:
                pair_addr = factory.functions.allPairs(idx).call()
            except (HTTPError, ContractLogicError) as e:
                print(f"[WARN] allPairs({idx}) failed ({e}), sleeping {RATE_LIMIT_SLEEP}s")
                time.sleep(RATE_LIMIT_SLEEP)
                continue

            low = pair_addr.lower()
            if low in seen:
                continue

            # fetch token0, token1, reserves
            pair = w3.eth.contract(address=pair_addr, abi=UNISWAP_PAIR_ABI)
            try:
                t0 = pair.functions.token0().call()
                t1 = pair.functions.token1().call()
                r0, r1, _ = pair.functions.getReserves().call()
            except (BadFunctionCallOutput, ContractLogicError, HTTPError) as e:
                print(f"[WARN] {pair_addr} pair call failed ({e}), skipping")
                continue

            # fetch token metadata
            tok0 = w3.eth.contract(address=t0, abi=ERC20_ABI)
            tok1 = w3.eth.contract(address=t1, abi=ERC20_ABI)
            try:
                dec0 = tok0.functions.decimals().call()
                sym0 = tok0.functions.symbol().call()
                dec1 = tok1.functions.decimals().call()
                sym1 = tok1.functions.symbol().call()
            except (BadFunctionCallOutput, ContractLogicError, HTTPError):
                dec0 = dec1 = None
                sym0 = sym1 = None

            ts = w3.eth.get_block("latest")["timestamp"]
            rec = {
                "pairAddress":     pair_addr,
                "token0":          t0,
                "token1":          t1,
                "reserve0":        str(r0),
                "reserve1":        str(r1),
                "blockTimestamp":  ts,
                "token0_symbol":   sym0,
                "token1_symbol":   sym1,
                "token0_decimals": dec0,
                "token1_decimals": dec1
            }

            append_record(rec)
            seen.add(low)
            added += 1
            print(f"[ADDED] {pair_addr}")

        time.sleep(SLEEP_BETWEEN_BATCHES)

    print(f"[DONE] {added} new pairs appended")

if __name__ == "__main__":
    print("🔄 Starting full-pair scanner (runs forever)")
    while True:
        try:
            fetch_all_pairs()
        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
        print(f"[INFO] Sleeping {FULL_SCAN_INTERVAL}s before next scan\n")
        time.sleep(FULL_SCAN_INTERVAL)