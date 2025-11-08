#!/usr/bin/env python3
"""
Arbitrage scanner – dual-RPC edition (with logging)
---------------------------------------------------

Keys stay embedded but everything is still override-able by env vars.

Quick start
-----------
$ LOG_LEVEL=DEBUG LOG_RPC=1 python3 arb_scanner.py
"""

import os, time, json, math, traceback, logging, datetime
from pathlib import Path
from typing import List, Tuple, Optional

from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

# ───────────────────────────── GLOBAL RPC KEYS ─────────────────────────────
DEFAULT_INFURA_KEY = "96b52d8457dd4a8494b4f985a331a3c1"
DEFAULT_TATUM_KEY  = "t-6859e1d47b2cac50cedeba0a-ed669e7df8fb45b5a993d267"

# ───────────────────────────── LOGGING SETUP ───────────────────────────────
level_name = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=getattr(logging, level_name, logging.INFO),
)
log = logging.getLogger("arb")

LOG_RPC       = os.getenv("LOG_RPC",       "0") == "1"
LOG_ARBITRAGE = os.getenv("LOG_ARBITRAGE", "0") == "1"

# ─────────────────────────── RPC AUTO-SELECTION ────────────────────────────
def make_provider() -> Web3:
    pref = os.getenv("PREFERRED_RPC", "infura").lower()
    tried = []

    def _connect(uri: str, **kwargs) -> Optional[Web3]:
        t0 = time.perf_counter()
        w3 = Web3(Web3.HTTPProvider(uri, request_kwargs=kwargs))
        ok = w3.is_connected()
        dur = (time.perf_counter() - t0) * 1e3
        log.debug("probe %s -> %s (%.1f ms)", uri, ok, dur)
        return w3 if ok else None

    # 1) Infura
    if pref in ("infura", "auto"):
        pid  = os.getenv("INFURA_PROJECT_ID", DEFAULT_INFURA_KEY)
        auth = os.getenv("INFURA_PROJECT_SECRET")
        uri  = f"https://{pid}:{auth}@mainnet.infura.io/v3/{pid}" if auth \
               else f"https://mainnet.infura.io/v3/{pid}"
        tried.append(uri)
        if w3 := _connect(uri):
            log.info("Connected to Infura – %s", uri)
            return w3

    # 2) Tatum
    if pref in ("tatum", "auto"):
        tatum_key = os.getenv("TATUM_API_KEY", DEFAULT_TATUM_KEY)
        network   = os.getenv("TATUM_NETWORK", "ethereum-mainnet")
        uri       = f"https://{network}.gateway.tatum.io"
        tried.append(uri)
        if w3 := _connect(uri, headers={"x-api-key": tatum_key}):
            log.info("Connected to Tatum  – %s", uri)
            return w3

    raise RuntimeError("Unable to connect to any RPC endpoint:\n  " + "\n  ".join(tried))

w3 = make_provider()

# Optional: dump every low-level JSON-RPC call/response
if LOG_RPC:
    from web3.middleware import construct_latest_block_based_cache_middleware
    w3.middleware_onion.clear()
    w3.middleware_onion.add(lambda make_request, w3:  # type: ignore
        (lambda m, p:
            (log.debug("→ %s %s", m, p), make_request(m, p))[1]) )
    w3.middleware_onion.add(construct_latest_block_based_cache_middleware)
    w3.middleware_onion.add(lambda make_request, w3:  # type: ignore
        (lambda m, p:
            (lambda r: (log.debug("← %s  %s", m, r), r)[1])
            (make_request(m, p))) )

# ─── LOAD ABIs ──────────────────────────────────────────────────────────────
ABI_FILE = Path(__file__).with_name("abis.json")
with ABI_FILE.open() as f:
    abis = json.load(f)
UNISWAP_PAIR_ABI = abis["UNISWAP_PAIR_ABI"]
ERC20_ABI        = abis["ERC20_ABI"]

# ─── POOL LIST ─────────────────────────────────────────────────────────────
PAIRS_FILE = Path(__file__).with_name("abis.txt")

def load_pool_addresses() -> List[str]:
    try:
        lines = PAIRS_FILE.read_text().splitlines()
    except Exception as e:
        log.error("Error reading %s: %s", PAIRS_FILE, e)
        return []
    addrs = []
    for line in lines:
        try:
            data = json.loads(line.strip())
            if addr := data.get("pairAddress"):
                addrs.append(Web3.to_checksum_address(addr))
        except json.JSONDecodeError:
            continue
    log.debug("Loaded %d pool addresses", len(addrs))
    return addrs

# ─── UTILS ─────────────────────────────────────────────────────────────────
def fetch_token_info(addr: str) -> dict:
    token = w3.eth.contract(address=addr, abi=ERC20_ABI)
    info  = {"address": addr}
    try:    info["symbol"]   = token.functions.symbol().call()
    except (BadFunctionCallOutput, ContractLogicError): info["symbol"] = None
    try:    info["decimals"] = token.functions.decimals().call()
    except (BadFunctionCallOutput, ContractLogicError): info["decimals"] = None
    return info

# ─── POOL OBJECT ───────────────────────────────────────────────────────────
class Pool:
    __slots__ = ("address","contract","token0","token1","token0_symbol","token1_symbol",
                 "token0_decimals","token1_decimals","reserve0","reserve1","is_valid")
    def __init__(self, addr: str):
        self.address = addr
        self.contract = w3.eth.contract(address=addr, abi=UNISWAP_PAIR_ABI)
        self.token0 = self.token1 = None
        self.token0_symbol = self.token1_symbol = None
        self.token0_decimals = self.token1_decimals = None
        self.reserve0 = self.reserve1 = 0.0
        self.is_valid = True

    def update(self):
        if not self.is_valid:
            return
        if self.token0 is None:
            try:
                self.token0 = self.contract.functions.token0().call()
                self.token1 = self.contract.functions.token1().call()
                i0, i1 = fetch_token_info(self.token0), fetch_token_info(self.token1)
                self.token0_symbol, self.token0_decimals = i0["symbol"], i0["decimals"]
                self.token1_symbol, self.token1_decimals = i1["symbol"], i1["decimals"]
            except (BadFunctionCallOutput, ContractLogicError) as e:
                log.warning("Invalid pair %s: %s", self.address, e)
                self.is_valid = False
                return
        try:
            r0, r1, _ = self.contract.functions.getReserves().call()
            if self.token0_decimals: r0 /= 10 ** self.token0_decimals
            if self.token1_decimals: r1 /= 10 ** self.token1_decimals
            self.reserve0, self.reserve1 = r0, r1
        except (BadFunctionCallOutput, ContractLogicError) as e:
            log.warning("Reserve fetch failed %s: %s", self.address, e)
            self.is_valid = False

    def price0_to_1(self):
        return 0 if self.reserve0 == 0 else (self.reserve1 / self.reserve0) * 0.997
    def price1_to_0(self):
        return 0 if self.reserve1 == 0 else (self.reserve0 / self.reserve1) * 0.997

# ─── GRAPH BUILD / SEARCH ──────────────────────────────────────────────────
def build_graph(pools: List[Pool]):
    nodes, edges = set(), []
    for p in pools:
        p.update()
        if not p.is_valid: continue
        t0, t1 = p.token0, p.token1
        nodes.add(t0); nodes.add(t1)
        if (p01 := p.price0_to_1()) > 0: edges.append((t0,t1,-math.log(p01),p))
        if (p10 := p.price1_to_0()) > 0: edges.append((t1,t0,-math.log(p10),p))
    if LOG_ARBITRAGE:
        log.debug("Graph built: %d nodes, %d edges", len(nodes), len(edges))
    return nodes, edges

def find_negative_cycle(nodes:set, edges:List[Tuple[str,str,float,Pool]]):
    idx = {t:i for i,t in enumerate(nodes)}
    inv = {i:t for t,i in idx.items()}
    dist = [0.0]*len(nodes); parent=[None]*len(nodes)

    for _ in range(len(nodes)-1):
        for u,v,w,p in edges:
            ui,vi = idx[u],idx[v]
            if dist[ui]+w < dist[vi]:
                dist[vi]=dist[ui]+w; parent[vi]=(ui,p)

    for u,v,w,p in edges:
        ui,vi = idx[u],idx[v]
        if dist[ui]+w < dist[vi]:
            # backtrack cycle
            for _ in range(len(nodes)): vi = parent[vi][0]
            cur, cycle, pools = vi, [], []
            while True:
                pu = parent[cur]
                if not pu: break
                cur_prev,pool=pu
                cycle.append(inv[cur]); pools.append(pool)
                cur=cur_prev
                if cur==vi: break
            cycle.append(inv[vi]); cycle.reverse(); pools.reverse()
            return cycle, pools
    return [],[]

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────
def monitor(interval=1.0):
    seen=set()
    log.info("Starting monitor every %.1f s (press Ctrl-C to exit)", interval)
    while True:
        try:
            pools=[Pool(a) for a in load_pool_addresses()]
            nodes,edges=build_graph(pools)
            cyc,cycp=find_negative_cycle(nodes,edges)
            if cyc:
                key="→".join(cyc)
                if key not in seen or LOG_ARBITRAGE:
                    seen.add(key)
                    gain=math.exp(-next(w for u,v,w,_ in edges if u==cyc[0] and v==cyc[1]))
                    log.warning("ARB %s  gain×%.6f", key, gain)
                    for p in cycp:
                        log.warning("   %s/%s @ %s", p.token0_symbol, p.token1_symbol, p.address)
        except Exception:
            log.exception("Error in monitor loop")
        time.sleep(interval)

if __name__ == "__main__":
    try:
        monitor()
    except KeyboardInterrupt:
        log.info("Exiting…")