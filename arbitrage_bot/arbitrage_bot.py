#!/usr/bin/env python3
import os
import time
import math
import json
from pathlib import Path
from typing import List, Tuple, Optional

from web3 import Web3
from web3.exceptions import BadFunctionCallOutput

# ─── CONFIGURE YOUR ETHEREUM RPC URL ───
WEB3_PROVIDER_URI = os.getenv(
    "WEB3_PROVIDER_URI",
    "https://mainnet.infura.io/v3/6f29b02833ab4700bc541dc4e436a35f"
)
w3 = Web3(Web3.HTTPProvider(WEB3_PROVIDER_URI))
if not w3.is_connected():
    raise RuntimeError("Unable to connect to Ethereum RPC – check your WEB3_PROVIDER_URI")

# ─── LOAD YOUR CONTRACT ABIs ───
#   Put exactly this JSON in `abis.json`:
#   {
#     "UNISWAP_PAIR_ABI": [ … ],
#     "ERC20_ABI": [ … ]
#   }
ABI_FILE   = Path(__file__).with_name("abis.json")
PAIRS_FILE = Path(__file__).with_name("abis.txt")   # your line-by-line pair data

with ABI_FILE.open() as f:
    abis = json.load(f)

UNISWAP_PAIR_ABI = abis["UNISWAP_PAIR_ABI"]
ERC20_ABI        = abis["ERC20_ABI"]

# ─── DYNAMICALLY READ ALL POOLS FROM YOUR PAIRS FILE ───
#   (each line: {"pairAddress":"0x…", …})
POOL_ADDRESSES: List[str] = []
if PAIRS_FILE.exists():
    for line in PAIRS_FILE.read_text().splitlines():
        try:
            obj = json.loads(line)
            addr = obj.get("pairAddress")
            if addr:
                POOL_ADDRESSES.append(Web3.to_checksum_address(addr))
        except json.JSONDecodeError:
            continue

if not POOL_ADDRESSES:
    print("⚠️  Warning: no pool addresses found in abis.txt – nothing to monitor.")

# ─── UTILITY TO FETCH TOKEN METADATA ───
def fetch_token_info(token_address: str) -> dict:
    token = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    return {
        "symbol":   token.functions.symbol().call(),
        "decimals": token.functions.decimals().call()
    }

# ─── POOL WRAPPER ───
class Pool:
    def __init__(self, address: str):
        self.address = address
        self.contract = w3.eth.contract(address=address, abi=UNISWAP_PAIR_ABI)
        self.token0: Optional[str] = None
        self.token1: Optional[str] = None
        self.token0_decimals: Optional[int] = None
        self.token1_decimals: Optional[int] = None
        self.token0_symbol: Optional[str] = None
        self.token1_symbol: Optional[str] = None
        self.reserve0: float = 0.0
        self.reserve1: float = 0.0
        self.is_valid_pair: bool = True

    def update_tokens_and_reserves(self) -> None:
        if not self.is_valid_pair:
            return

        # fetch token addresses + metadata
        try:
            if self.token0 is None:
                self.token0 = self.contract.functions.token0().call()
                self.token1 = self.contract.functions.token1().call()
                info0 = fetch_token_info(self.token0)
                info1 = fetch_token_info(self.token1)
                self.token0_decimals = info0["decimals"]
                self.token1_decimals = info1["decimals"]
                self.token0_symbol   = info0["symbol"]
                self.token1_symbol   = info1["symbol"]
        except BadFunctionCallOutput as e:
            # print(f"⚠️  {self.address} invalid token0/token1 – skipping ({e})")
            self.is_valid_pair = False
            return

        # fetch reserves
        try:
            r0, r1, _ = self.contract.functions.getReserves().call()
            self.reserve0 = r0 / (10 ** self.token0_decimals)
            self.reserve1 = r1 / (10 ** self.token1_decimals)
        except BadFunctionCallOutput as e:
            print(f"⚠️  {self.address} invalid getReserves – skipping ({e})")
            self.is_valid_pair = False

    def price_token0_to_token1(self) -> float:
        return 0.0 if self.reserve0 == 0 else (self.reserve1 / self.reserve0) * 0.997

    def price_token1_to_token0(self) -> float:
        return 0.0 if self.reserve1 == 0 else (self.reserve0 / self.reserve1) * 0.997

    def tokens(self) -> Tuple[str, str]:
        return (self.token0, self.token1)

    def symbols(self) -> Tuple[str, str]:
        return (self.token0_symbol, self.token1_symbol)

# ─── BUILD GRAPH FOR ARBITRAGE ───
def build_rate_graph(
    pools: List[Pool]
) -> Tuple[set, List[Tuple[str, str, float, Pool]]]:
    nodes = set()
    edges = []
    for pool in pools:
        pool.update_tokens_and_reserves()
        if not pool.is_valid_pair:
            continue
        t0, t1 = pool.tokens()
        nodes.update([t0, t1])
        p01 = pool.price_token0_to_token1()
        p10 = pool.price_token1_to_token0()
        if p01 > 0:
            edges.append((t0, t1, -math.log(p01), pool))
        if p10 > 0:
            edges.append((t1, t0, -math.log(p10), pool))
    return nodes, edges

# ─── DETECT NEGATIVE CYCLE VIA BELLMAN–FORD ───
def find_negative_cycle(
    nodes: set,
    edges: List[Tuple[str, str, float, Pool]]
) -> Tuple[List[str], List[Pool]]:
    token_to_idx = {t: i for i, t in enumerate(nodes)}
    idx_to_token = {i: t for t, i in token_to_idx.items()}
    dist   = [0.0] * len(nodes)
    parent = [None] * len(nodes)

    N = len(nodes)
    for _ in range(N - 1):
        updated = False
        for u, v, w, pool in edges:
            ui, vi = token_to_idx[u], token_to_idx[v]
            if dist[ui] + w < dist[vi]:
                dist[vi]   = dist[ui] + w
                parent[vi] = (ui, pool)
                updated = True
        if not updated:
            break

    for u, v, w, pool in edges:
        ui, vi = token_to_idx[u], token_to_idx[v]
        if dist[ui] + w < dist[vi]:
            # found a negative cycle
            cycle_tokens = []
            cycle_pools  = []
            start = vi
            for _ in range(N):
                prev = parent[start]
                if not prev:
                    break
                start = prev[0]
            cur = start
            while True:
                prev = parent[cur]
                if not prev:
                    break
                prev_idx, pl = prev
                cycle_tokens.append(idx_to_token[cur])
                cycle_pools.append(pl)
                cur = prev_idx
                if cur == start:
                    break
            cycle_tokens.append(idx_to_token[start])
            cycle_tokens.reverse()
            cycle_pools.reverse()
            return cycle_tokens, cycle_pools
    return [], []

# ─── MAIN LOOP: POLL & REPORT ───
def monitor_arbitrage(poll_interval: float = 10.0) -> None:
    pools = [Pool(addr) for addr in POOL_ADDRESSES]
    seen_cycles = set()
    print(f"🚀 Monitoring {len(pools)} pools every {poll_interval}s…\n")
    while True:
        nodes, edges = build_rate_graph(pools)
        cycle_tokens, cycle_pools = find_negative_cycle(nodes, edges)
        if cycle_tokens:
            key = " → ".join(cycle_tokens)
            if key not in seen_cycles:
                seen_cycles.add(key)
                # compute gross multiplier
                w = next(w for u, v, w, _ in edges if u == cycle_tokens[0] and v == cycle_tokens[1])
                gain = math.exp(-w)
                print(f"🔥 Arbitrage! {key}  (x{gain:.6f})")
                for p in cycle_pools:
                    s0, s1 = p.symbols()
                    print(f"    • {s0}/{s1} @ {p.address}")
                print()
        time.sleep(poll_interval)

if __name__ == "__main__":
    try:
        monitor_arbitrage(poll_interval=1.0)
    except KeyboardInterrupt:
        print("\n👋 Exiting arbitrage monitor.")