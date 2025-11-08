#!/usr/bin/env python3
import os
import sys
import json
import time
from pathlib import Path
from web3 import Web3
from web3._utils.events import get_event_data

# ——— CONFIGURATION ———
RPC_URL = os.getenv(
    "WEB3_PROVIDER_URI",
    "https://mainnet.infura.io/v3/4c80840994fa41d69aebb9d29d42ed8b"
)
if not RPC_URL:
    print("ERROR: WEB3_PROVIDER_URI not set. Export your Ethereum RPC endpoint and try again.")
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    print(f"ERROR: Unable to connect to RPC at {RPC_URL}")
    sys.exit(1)

FACTORY_ADDRESS = w3.to_checksum_address("0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f")
ABIS_FILE       = Path(__file__).with_name("abis.txt")
LAST_BLOCK_FILE = Path(__file__).with_name("last_block.txt")

# ——— ABIs ———
FACTORY_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True,  "internalType": "address", "name": "token0", "type": "address"},
        {"indexed": True,  "internalType": "address", "name": "token1", "type": "address"},
        {"indexed": False, "internalType": "address", "name": "pair",   "type": "address"},
        {"indexed": False, "internalType": "uint256", "name": "",       "type": "uint256"}
    ],
    "name": "PairCreated",
    "type": "event"
}]

UNISWAP_PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ——— HELPERS ———
def load_seen_pairs() -> set:
    seen = set()
    if not ABIS_FILE.exists():
        return seen
    for line in ABIS_FILE.read_text().splitlines():
        try:
            rec = json.loads(line)
            addr = rec.get("pairAddress", "").lower()
            if addr:
                seen.add(addr)
        except json.JSONDecodeError:
            continue
    return seen

def load_last_block() -> int:
    if LAST_BLOCK_FILE.exists():
        return int(LAST_BLOCK_FILE.read_text().strip())
    return w3.eth.block_number - 100  # start 100 blocks back on first run

def save_last_block(block: int):
    LAST_BLOCK_FILE.write_text(str(block))

def append_record(rec: dict):
    with ABIS_FILE.open("a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")

# ——— MAIN LOOP ———
def run_scanner():
    seen      = load_seen_pairs()
    start_blk = load_last_block() + 1
    end_blk   = w3.eth.block_number

    print(f"[INFO] Scanning blocks {start_blk} → {end_blk}")

    topic = "0x" + w3.keccak(text="PairCreated(address,address,address,uint256)").hex()
    try:
        logs = w3.eth.get_logs({
            "fromBlock": start_blk,
            "toBlock":   end_blk,
            "address":   FACTORY_ADDRESS,
            "topics":    [topic],
        })
    except Exception as e:
        print(f"[ERROR] get_logs failed: {e}")
        return

    added = 0
    for log in logs:
        ev = get_event_data(w3.codec, FACTORY_ABI[0], log)
        pair_addr = ev["args"]["pair"]
        if pair_addr.lower() in seen:
            continue

        pair = w3.eth.contract(address=pair_addr, abi=UNISWAP_PAIR_ABI)
        r0, r1, _ = pair.functions.getReserves().call()
        t0        = pair.functions.token0().call()
        t1        = pair.functions.token1().call()

        tok0 = w3.eth.contract(address=t0, abi=ERC20_ABI)
        tok1 = w3.eth.contract(address=t1, abi=ERC20_ABI)
        try:
            dec0 = tok0.functions.decimals().call()
            dec1 = tok1.functions.decimals().call()
            sym0 = tok0.functions.symbol().call()
            sym1 = tok1.functions.symbol().call()
        except:
            dec0 = dec1 = sym0 = sym1 = None

        block = w3.eth.get_block(log["blockNumber"])
        rec = {
            "pairAddress":    pair_addr,
            "token0":         t0,
            "token1":         t1,
            "reserve0":       str(r0),
            "reserve1":       str(r1),
            "blockTimestamp": block["timestamp"],
            "token0_symbol":  sym0,
            "token1_symbol":  sym1,
            "token0_decimals":dec0,
            "token1_decimals":dec1,
        }

        append_record(rec)
        print(f"[ADDED] {pair_addr}")
        added += 1

    if added:
        save_last_block(end_blk)
    print(f"[INFO] {added} new pair(s) recorded this cycle.\n")

if __name__ == "__main__":
    print("🔄 Starting Uniswap pair scanner (runs forever, CTRL+C to exit)")
    while True:
        run_scanner()
        time.sleep(15)